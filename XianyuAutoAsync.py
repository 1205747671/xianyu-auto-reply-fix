import asyncio
import json
import re
import time
import base64
import hashlib
import os
import random
import secrets
import sys
import threading
from enum import Enum
from urllib.parse import parse_qs, urlparse
from loguru import logger
import websockets
from utils.xianyu_utils import (
    decrypt, generate_mid, generate_uuid, trans_cookies,
    generate_device_id, generate_sign
)
from config import (
    WEBSOCKET_URL, HEARTBEAT_INTERVAL, HEARTBEAT_TIMEOUT,
    TOKEN_REFRESH_INTERVAL, TOKEN_RETRY_INTERVAL,
    SESSION_KEEPALIVE_INTERVAL, SESSION_KEEPALIVE_RETRY_INTERVAL,
    LOG_CONFIG, AUTO_REPLY, DEFAULT_HEADERS, WEBSOCKET_HEADERS,
    APP_CONFIG, API_ENDPOINTS, YIFAN_API
)
import aiohttp
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Tuple
from db_manager import db_manager
from utils.notification_dispatcher import (
    dispatch_account_notifications,
    format_notification_template,
    get_notification_template_text,
    guess_verification_type,
    render_notification_template,
)
from utils.account_browser_runtime import account_browser_runtime_manager


DELIVERY_BATCH_MAX_UNITS = 10
DELIVERY_BATCH_MAX_CHARS = 1200


def _resolve_websocket_open_timeout(default: int = 30) -> int:
    raw = os.environ.get('XY_WS_OPEN_TIMEOUT')
    if raw is None or str(raw).strip() == '':
        return default
    try:
        return max(5, int(float(raw)))
    except (TypeError, ValueError):
        return default


WEBSOCKET_OPEN_TIMEOUT = _resolve_websocket_open_timeout()
PROTECTED_SESSION_COOKIE_FIELDS = (
    'unb',
    'sgcookie',
    'cookie2',
    '_m_h5_tk',
    '_m_h5_tk_enc',
    't',
    'cna',
    'havana_lgc2_77',
    '_tb_token_',
)
REQUIRED_SESSION_COOKIE_FIELDS = (
    'unb',
    'sgcookie',
    'cookie2',
    '_m_h5_tk',
    '_m_h5_tk_enc',
    't',
    'cna',
)
OBSERVED_SESSION_COOKIE_FIELDS = ()


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"
    CLOSED = "closed"

class InitAuthError(Exception):
    pass


class AutoReplyPauseManager:
    def __init__(self):
        self.paused_chats = {}

    @staticmethod
    def _resolve_account_scope(account_id: Any = None) -> str:
        normalized = str(account_id or "").strip()
        if normalized:
            return normalized
        return ""

    def _compose_pause_scope_key(
        self,
        chat_id: Any,
        account_id: Any = None,
    ):
        resolved_account_id = str(account_id or "").strip()
        normalized_chat_id = str(chat_id or "").strip()
        if resolved_account_id == "default":
            resolved_account_id = ""
        if not resolved_account_id or not normalized_chat_id:
            return None
        return (resolved_account_id, normalized_chat_id)

    def pause_chat(self, chat_id: str, account_id: str = None):
        resolved_account_id = str(account_id or "").strip()
        if resolved_account_id == "default":
            resolved_account_id = ""
        account_label = resolved_account_id or "default"
        pause_scope_key = self._compose_pause_scope_key(
            chat_id,
            account_id,
        )

        if not pause_scope_key:
            logger.warning(
                f"【default】手动发消息暂停缺少 canonical account_id 或 chat_id，跳过记录暂停状态: chat_id={chat_id}"
            )
            return

        try:
            from db_manager import db_manager
            pause_minutes = db_manager.get_cookie_pause_duration(resolved_account_id)
        except Exception as e:
            logger.error(f"获取账号 {account_label} 暂停时间失败: {e}，使用默认10分钟")
            pause_minutes = 10

        if pause_minutes == 0:
            logger.info(f"【{account_label}】检测到手动发出消息，但暂停时间设置为0，不暂停自动回复")
            return

        pause_duration_seconds = pause_minutes * 60
        pause_until = time.time() + pause_duration_seconds
        self.paused_chats[pause_scope_key] = pause_until

        end_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pause_until))
        logger.info(f"【{account_label}】检测到手动发出消息，chat_id {chat_id} 自动回复暂停{pause_minutes}分钟，恢复时间: {end_time}")

    def is_chat_paused(self, chat_id: str, account_id: str = None) -> bool:
        pause_scope_key = self._compose_pause_scope_key(
            chat_id,
            account_id,
        )
        if not pause_scope_key or pause_scope_key not in self.paused_chats:
            return False

        current_time = time.time()
        pause_until = self.paused_chats[pause_scope_key]

        if current_time >= pause_until:
            return False

        return True

    def get_remaining_pause_time(self, chat_id: str, account_id: str = None) -> int:
        pause_scope_key = self._compose_pause_scope_key(
            chat_id,
            account_id,
        )
        if not pause_scope_key or pause_scope_key not in self.paused_chats:
            return 0

        current_time = time.time()
        pause_until = self.paused_chats[pause_scope_key]
        remaining = max(0, int(pause_until - current_time))

        return remaining

    def cleanup_expired_pauses(self):
        current_time = time.time()
        expired_chats = [
            pause_scope_key
            for pause_scope_key, pause_until in self.paused_chats.items()
            if current_time >= pause_until
        ]

        for pause_scope_key in expired_chats:
            del self.paused_chats[pause_scope_key]



pause_manager = AutoReplyPauseManager()


def log_captcha_event(
    account_id: str = None,
    event_type: str = "",
    success: bool = None,
    details: str = "",
):
    try:
        resolved_account_id = AutoReplyPauseManager._resolve_account_scope(
            account_id,
        )
        account_label = resolved_account_id or "default"
        log_dir = 'logs'
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, 'captcha_verification.txt')

        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        status = "成功" if success is True else "失败" if success is False else "进行中"

        log_entry = f"[{timestamp}] 【{account_label}】{event_type} - {status}"
        if details:
            log_entry += f" - {details}"
        log_entry += "\n"

        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(log_entry)

    except Exception as e:
        logger.error(f"记录滑块验证日志失败: {e}")

# setup_logging(LOG_CONFIG)  # 已移除，模块不存在
class XianyuLive:
    _order_locks = defaultdict(lambda: asyncio.Lock())
    _lock_usage_times = {}
    _lock_hold_info = {}

    _order_detail_locks = defaultdict(lambda: asyncio.Lock())
    _order_detail_lock_times = {}

    _item_detail_cache = {}
    _item_detail_cache_lock = asyncio.Lock()
    _item_detail_cache_max_size = 1000
    _item_detail_cache_ttl = 24 * 60 * 60

    _instances = {}
    _instances_lock = asyncio.Lock()

    _last_password_login_time = {}
    _password_login_cooldown = 60
    _password_login_failure_backoff = {}

    _manual_refresh_state = {}
    _manual_refresh_lock = threading.Lock()
    _manual_refresh_handoff_ttl = 120
    _auth_recovery_locks = {}
    _auth_recovery_lock = threading.Lock()
    _auth_recovery_lock_ttl = 240

    _auth_prewarmed_tokens = {}
    _auth_prewarmed_token_ttl = 180
    _last_risk_log_cleanup_times = {}
    _risk_log_cleanup_locks = {}

    _init_auth_failure_state = {}
    _init_auth_failure_lock = threading.Lock()
    _init_auth_failure_window = 60
    _init_auth_failure_threshold = 3
    _init_auth_cooldown = 60

    _qr_prewarmed_tokens = {}
    _qr_prewarmed_token_ttl = 180
    _qr_login_grace_state = {}
    _qr_login_grace_ttl = 180
    @classmethod
    def _normalize_account_scope(cls, account_id: Any = None) -> str:
        normalized = str(account_id or "").strip()
        if normalized:
            return normalized
        return ""

    @classmethod
    def _normalize_manual_refresh_account_scope(cls, account_id: Any = None) -> str:
        resolved_account_id = cls._normalize_account_scope(account_id)
        if resolved_account_id == "default":
            return ""
        return resolved_account_id

    @classmethod
    def _cleanup_auth_prewarmed_tokens(cls):
        now = time.time()
        expired_account_ids = [
            account_id
            for account_id, token_info in cls._auth_prewarmed_tokens.items()
            if now - token_info.get('timestamp', 0) > cls._auth_prewarmed_token_ttl
        ]
        for account_id in expired_account_ids:
            cls._auth_prewarmed_tokens.pop(account_id, None)

    @classmethod
    def cache_auth_prewarmed_token(
        cls,
        account_id: str = None,
        token: str = None,
        source: str = 'generic_auth',
    ):
        resolved_account_id = cls._normalize_account_scope(account_id)
        if not resolved_account_id or not token:
            return
        cls._cleanup_auth_prewarmed_tokens()
        cls._auth_prewarmed_tokens[resolved_account_id] = {
            'token': token,
            'timestamp': time.time(),
            'source': source,
        }

    @classmethod
    def pop_auth_prewarmed_token(
        cls,
        account_id: str = None,
    ) -> Optional[Dict[str, Any]]:
        resolved_account_id = cls._normalize_account_scope(account_id)
        if not resolved_account_id:
            return None
        cls._cleanup_auth_prewarmed_tokens()
        token_info = cls._auth_prewarmed_tokens.pop(resolved_account_id, None)
        if not token_info:
            return None
        if time.time() - token_info.get('timestamp', 0) > cls._auth_prewarmed_token_ttl:
            return None
        return token_info

    @classmethod
    def clear_auth_prewarmed_token(
        cls,
        account_id: str = None,
    ):
        resolved_account_id = cls._normalize_account_scope(account_id)
        if not resolved_account_id:
            return
        cls._auth_prewarmed_tokens.pop(resolved_account_id, None)

    @classmethod
    def _cleanup_qr_prewarmed_tokens(cls):
        now = time.time()
        expired_account_ids = [
            account_id
            for account_id, token_info in cls._qr_prewarmed_tokens.items()
            if now - token_info.get('timestamp', 0) > cls._qr_prewarmed_token_ttl
        ]
        for account_id in expired_account_ids:
            cls._qr_prewarmed_tokens.pop(account_id, None)

    @classmethod
    def cache_qr_prewarmed_token(
        cls,
        account_id: str = None,
        token: str = None,
    ):
        resolved_account_id = cls._normalize_account_scope(account_id)
        if not resolved_account_id or not token:
            return
        cls._cleanup_qr_prewarmed_tokens()
        cls._qr_prewarmed_tokens[resolved_account_id] = {
            'token': token,
            'timestamp': time.time()
        }

    @classmethod
    def pop_qr_prewarmed_token(
        cls,
        account_id: str = None,
    ) -> Optional[Dict[str, Any]]:
        resolved_account_id = cls._normalize_account_scope(account_id)
        if not resolved_account_id:
            return None
        cls._cleanup_qr_prewarmed_tokens()
        token_info = cls._qr_prewarmed_tokens.pop(resolved_account_id, None)
        if not token_info:
            return None
        if time.time() - token_info.get('timestamp', 0) > cls._qr_prewarmed_token_ttl:
            return None
        return token_info

    @classmethod
    def clear_qr_prewarmed_token(
        cls,
        account_id: str = None,
    ):
        resolved_account_id = cls._normalize_account_scope(account_id)
        if not resolved_account_id:
            return
        cls._qr_prewarmed_tokens.pop(resolved_account_id, None)

    @classmethod
    def _cleanup_manual_refresh_state(cls):
        now = time.time()
        expired_account_ids = []
        with cls._manual_refresh_lock:
            for account_id, state in cls._manual_refresh_state.items():
                if state.get('phase') != 'handoff_recovery':
                    continue
                expires_at = state.get('expires_at', 0)
                if expires_at and now > expires_at:
                    expired_account_ids.append(account_id)

            for account_id in expired_account_ids:
                cls._manual_refresh_state.pop(account_id, None)

        for account_id in expired_account_ids:
            logger.warning(f"【{account_id}】刷新交接恢复状态已过期，自动清理")

    @classmethod
    def get_manual_refresh_state(
        cls,
        account_id: str = None,
    ) -> Optional[Dict[str, Any]]:
        resolved_account_id = cls._normalize_manual_refresh_account_scope(account_id)
        if not resolved_account_id:
            return None
        cls._cleanup_manual_refresh_state()
        with cls._manual_refresh_lock:
            state = cls._manual_refresh_state.get(resolved_account_id)
            return dict(state) if state else None

    @classmethod
    def mark_manual_refresh_handoff(
        cls,
        account_id: str = None,
        source: str = 'manual_refresh_handoff',
        ttl: int = None,
    ) -> Dict[str, Any]:
        resolved_account_id = cls._normalize_manual_refresh_account_scope(account_id)
        if not resolved_account_id:
            return {'updated': False, 'reason': 'empty_account_id'}

        live_instance = cls.get_instance(account_id=resolved_account_id)
        previous_cookie_refresh_enabled = None
        if live_instance is not None:
            previous_cookie_refresh_enabled = live_instance.cookie_refresh_enabled

        now = time.time()
        expires_at = now + (ttl or cls._manual_refresh_handoff_ttl)
        with cls._manual_refresh_lock:
            state = cls._manual_refresh_state.get(resolved_account_id) or {}
            state.update({
                'source': source,
                'phase': 'handoff_recovery',
                'started_at': state.get('started_at', now),
                'updated_at': now,
                'handoff_started_at': now,
                'expires_at': expires_at,
                'slider_failed_bypass_used': state.get('slider_failed_bypass_used', False),
                'previous_cookie_refresh_enabled': state.get('previous_cookie_refresh_enabled', previous_cookie_refresh_enabled),
            })
            cls._manual_refresh_state[resolved_account_id] = state

        logger.warning(
            f"【{resolved_account_id}】已进入刷新交接恢复窗口，允许新实例执行初始化恢复（有效期 {int(expires_at - now)} 秒）"
        )
        return {'updated': True, 'phase': 'handoff_recovery', 'expires_at': expires_at}

    @classmethod
    def consume_manual_refresh_slider_failed_bypass(
        cls,
        account_id: str = None,
    ) -> bool:
        resolved_account_id = cls._normalize_manual_refresh_account_scope(account_id)
        if not resolved_account_id:
            return False
        cls._cleanup_manual_refresh_state()
        with cls._manual_refresh_lock:
            state = cls._manual_refresh_state.get(resolved_account_id)
            if not state or state.get('phase') != 'handoff_recovery':
                return False
            if state.get('slider_failed_bypass_used'):
                return False
            state['slider_failed_bypass_used'] = True
            state['updated_at'] = time.time()
            return True

    @classmethod
    def _cleanup_auth_recovery_locks(cls):
        now = time.time()
        expired_account_ids = []
        with cls._auth_recovery_lock:
            for account_id, state in cls._auth_recovery_locks.items():
                if now > state.get('expires_at', 0):
                    expired_account_ids.append(account_id)
            for account_id in expired_account_ids:
                cls._auth_recovery_locks.pop(account_id, None)

    @classmethod
    def acquire_auth_recovery_lock(
        cls,
        account_id: str = None,
        owner: str = None,
        ttl: int = None,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        resolved_account_id = cls._normalize_account_scope(account_id)
        if not resolved_account_id or not owner:
            return False, None
        cls._cleanup_auth_recovery_locks()
        now = time.time()
        expires_at = now + (ttl or cls._auth_recovery_lock_ttl)
        with cls._auth_recovery_lock:
            existing = cls._auth_recovery_locks.get(resolved_account_id)
            if existing and existing.get('owner') != owner and now <= existing.get('expires_at', 0):
                return False, dict(existing)
            cls._auth_recovery_locks[resolved_account_id] = {
                'owner': owner,
                'acquired_at': now,
                'expires_at': expires_at,
            }
        return True, None

    @classmethod
    def release_auth_recovery_lock(
        cls,
        account_id: str = None,
        owner: str = None,
    ):
        resolved_account_id = cls._normalize_account_scope(account_id)
        if not resolved_account_id:
            return
        with cls._auth_recovery_lock:
            existing = cls._auth_recovery_locks.get(resolved_account_id)
            if not existing:
                return
            if owner and existing.get('owner') != owner:
                return
            cls._auth_recovery_locks.pop(resolved_account_id, None)

    @classmethod
    def get_init_auth_failure_state(
        cls,
        account_id: str = None,
    ) -> Optional[Dict[str, Any]]:
        resolved_account_id = cls._normalize_account_scope(account_id)
        if not resolved_account_id:
            return None
        with cls._init_auth_failure_lock:
            state = cls._init_auth_failure_state.get(resolved_account_id)
            if not state:
                return None
            if state.get('circuit_until') and time.time() > state.get('circuit_until', 0):
                state = {
                    'count': 0,
                    'window_started_at': 0,
                    'last_failure_at': state.get('last_failure_at', 0),
                    'last_reason': state.get('last_reason'),
                    'circuit_until': 0,
                }
                cls._init_auth_failure_state[resolved_account_id] = state
            return dict(state)

    @classmethod
    def record_init_auth_failure(
        cls,
        account_id: str = None,
        reason: str = '',
    ) -> Dict[str, Any]:
        resolved_account_id = cls._normalize_account_scope(account_id)
        if not resolved_account_id:
            return {}
        now = time.time()
        with cls._init_auth_failure_lock:
            state = cls._init_auth_failure_state.get(resolved_account_id) or {
                'count': 0,
                'window_started_at': now,
                'last_failure_at': 0,
                'last_reason': '',
                'circuit_until': 0,
            }
            window_started_at = state.get('window_started_at', 0)
            if not window_started_at or (now - window_started_at) > cls._init_auth_failure_window:
                state['count'] = 0
                state['window_started_at'] = now
                state['circuit_until'] = 0

            state['count'] = int(state.get('count', 0)) + 1
            state['last_failure_at'] = now
            state['last_reason'] = str(reason or '')
            if state['count'] >= cls._init_auth_failure_threshold:
                state['circuit_until'] = now + cls._init_auth_cooldown

            cls._init_auth_failure_state[resolved_account_id] = state
            return dict(state)

    @classmethod
    def clear_init_auth_failure_state(
        cls,
        account_id: str = None,
    ):
        resolved_account_id = cls._normalize_account_scope(account_id)
        if not resolved_account_id:
            return
        with cls._init_auth_failure_lock:
            cls._init_auth_failure_state.pop(resolved_account_id, None)

    @classmethod
    def _cleanup_qr_login_grace_state(cls):
        now = time.time()
        expired_account_ids = [
            account_id
            for account_id, state in cls._qr_login_grace_state.items()
            if now - state.get('timestamp', 0) > cls._qr_login_grace_ttl
        ]
        for account_id in expired_account_ids:
            cls._qr_login_grace_state.pop(account_id, None)

    @staticmethod
    def _find_legacy_account_alias_key(values: Dict[str, Any]) -> Optional[str]:
        for key in values.keys():
            normalized_key = str(key or "").strip()
            if normalized_key.startswith('cookie') and normalized_key.endswith('_id'):
                return normalized_key
        return None

    @classmethod
    def mark_qr_login_grace(
        cls,
        account_id: str = None,
        **extra_state,
    ):
        legacy_alias_key = cls._find_legacy_account_alias_key(extra_state)
        if legacy_alias_key:
            raise TypeError(
                f"mark_qr_login_grace() got an unexpected keyword argument '{legacy_alias_key}'"
            )
        resolved_account_id = cls._normalize_account_scope(account_id)
        if not resolved_account_id:
            return
        cls._cleanup_qr_login_grace_state()
        state = {
            'timestamp': time.time(),
            'captcha_buffer_used': False,
            'browser_stabilized': False,
        }
        state.update(extra_state)
        cls._qr_login_grace_state[resolved_account_id] = state

    @classmethod
    def get_qr_login_grace(
        cls,
        account_id: str = None,
    ) -> Optional[Dict[str, Any]]:
        resolved_account_id = cls._normalize_account_scope(account_id)
        if not resolved_account_id:
            return None
        cls._cleanup_qr_login_grace_state()
        state = cls._qr_login_grace_state.get(resolved_account_id)
        if not state:
            return None
        if time.time() - state.get('timestamp', 0) > cls._qr_login_grace_ttl:
            cls._qr_login_grace_state.pop(resolved_account_id, None)
            return None
        return state

    @classmethod
    def update_qr_login_grace(
        cls,
        account_id: str = None,
        **updates,
    ):
        legacy_alias_key = cls._find_legacy_account_alias_key(updates)
        if legacy_alias_key:
            raise TypeError(
                f"update_qr_login_grace() got an unexpected keyword argument '{legacy_alias_key}'"
            )
        resolved_account_id = cls._normalize_account_scope(account_id)
        state = cls.get_qr_login_grace(account_id=resolved_account_id)
        if not state:
            return None
        state.update(updates)
        cls._qr_login_grace_state[resolved_account_id] = state
        return state

    @classmethod
    def clear_qr_login_grace(
        cls,
        account_id: str = None,
    ):
        resolved_account_id = cls._normalize_account_scope(account_id)
        if not resolved_account_id:
            return
        cls._qr_login_grace_state.pop(resolved_account_id, None)

    @classmethod
    def _cleanup_password_login_failure_backoff(cls):
        now = time.time()
        expired_account_ids = [
            account_id
            for account_id, state in cls._password_login_failure_backoff.items()
            if now >= state.get('until', 0)
        ]
        for account_id in expired_account_ids:
            cls._password_login_failure_backoff.pop(account_id, None)

    @classmethod
    def get_password_login_failure_backoff(
        cls,
        account_id: str = None,
    ) -> Optional[Dict[str, Any]]:
        resolved_account_id = cls._normalize_account_scope(account_id)
        if not resolved_account_id:
            return None
        cls._cleanup_password_login_failure_backoff()
        return cls._password_login_failure_backoff.get(resolved_account_id)

    @classmethod
    def clear_password_login_failure_backoff(
        cls,
        account_id: str = None,
    ):
        resolved_account_id = cls._normalize_account_scope(account_id)
        if not resolved_account_id:
            return
        cls._password_login_failure_backoff.pop(resolved_account_id, None)

    @classmethod
    def set_password_login_failure_backoff(
        cls,
        account_id: str = None,
        reason: str = '',
        seconds: int = 0,
    ):
        resolved_account_id = cls._normalize_account_scope(account_id)
        if not resolved_account_id or seconds <= 0:
            return
        cls._password_login_failure_backoff[resolved_account_id] = {
            'until': time.time() + seconds,
            'reason': reason,
            'seconds': seconds,
            'created_at': time.time(),
        }

    @staticmethod
    def _legacy_gbk_mojibake(text: str, replace_invalid: bool = False) -> str:
        errors = "replace" if replace_invalid else "strict"
        return text.encode("utf-8").decode("gbk", errors=errors).replace("\ufffd", "?")

    @staticmethod
    def _legacy_missing_tail(text: str) -> str:
        return f"{text[:-1]}?" if len(text) > 1 else text

    @staticmethod
    def _legacy_drop_last_char(text: str) -> str:
        return text[:-1] if text else text

    @staticmethod
    def _legacy_replace_tail(text: str, tail: str, replacement: str) -> str:
        if tail and text.endswith(tail):
            return f"{text[:-len(tail)]}{replacement}"
        return text

    @staticmethod
    def classify_password_login_failure(error_message: str) -> Tuple[str, int]:
        message = (error_message or "").lower()
        if any(keyword in message for keyword in ["账号密码错误", "账密错误", "用户名或密码错误", "密码错误"]):
            return "credentials", 1800
        if any(keyword in message for keyword in ["前置滑块", "风控", "拦截", "框体错误", "点击框体重试"]):
            return "risk_control", 900
        if any(
            keyword in message
            for keyword in [
                "滑块验证失败",
                XianyuLive._legacy_gbk_mojibake("滑块验证失败"),
                "未找到滑块容器",
                XianyuLive._legacy_missing_tail("未找到滑块容器"),
            ]
        ):
            return "slider_failed", 600
        if any(
            keyword in message for keyword in [
                "未找到登录表单",
                XianyuLive._legacy_missing_tail("未找到登录表单"),
                "未找到登录iframe",
                "session过期且清理会话状态后未找到登录表单",
                XianyuLive._legacy_missing_tail("session过期且清理会话状态后未找到登录表单"),
                "session验证异常且清理会话状态后未找到登录表单",
                XianyuLive._legacy_missing_tail("session验证异常且清理会话状态后未找到登录表单"),
            ]
        ):
            return "login_form_missing", 90
        if any(
            keyword in message
            for keyword in [
                "页面会话已失效",
                XianyuLive._legacy_missing_tail("页面会话已失效"),
                "target page, context or browser has been closed",
            ]
        ):
            return "unknown", 180
        if any(keyword in message for keyword in ["网络", "timeout", "cannot connect", "连接", "dns", "ssl"]):
            return "network", 180
        return "unknown", 300

    def _safe_str(self, e):
        try:
            return str(e)
        except Exception:
            try:
                return repr(e)
            except Exception:
                return "未知错误"

    def _mask_secret_value(self, value: str, head: int = 6, tail: int = 4) -> str:
        text = str(value or '')
        if not text:
            return ''
        if len(text) <= head + tail:
            return '***'
        return f"{text[:head]}***{text[-tail:]}"

    def _summarize_cookie_string(self, cookie_string: str) -> str:
        cookie_string = str(cookie_string or '').strip()
        if not cookie_string:
            return 'empty-cookie'

        segments = []
        for part in cookie_string.split(';'):
            part = part.strip()
            if not part:
                continue
            if '=' in part:
                key, value = part.split('=', 1)
                segments.append(f"{key.strip()}={self._mask_secret_value(value.strip(), head=4, tail=2)}")
            else:
                segments.append(self._mask_secret_value(part, head=4, tail=2))

        preview = '; '.join(segments[:6])
        if len(segments) > 6:
            preview += f"; ...(+{len(segments) - 6} fields)"
        return preview

    @staticmethod
    def _new_risk_session_id(prefix: str = 'risk') -> str:
        return f"{prefix}_{secrets.token_hex(8)}"

    def _normalize_risk_trigger_scene(self, trigger_reason: str = None, default: str = 'unknown') -> str:
        text = str(trigger_reason or '').strip()
        if not text:
            return default
        lower_text = text.lower()
        if 'token' in lower_text or 'session' in lower_text or '令牌' in text:
            return 'token_refresh'
        if 'password' in lower_text or '账密' in text or '登录' in text:
            return 'password_login'
        if 'cookie' in lower_text or '连接' in text or '失败' in text:
            return 'auto_cookie_refresh'
        return default

    def _sanitize_verification_meta(self, verification_url: str = None) -> Dict[str, Any]:
        text = str(verification_url or '').strip()
        if not text:
            return {}

        try:
            parsed = urlparse(text)
            if not parsed.scheme and not parsed.netloc:
                return {'verification_source': text[:120]}

            meta: Dict[str, Any] = {
                'verification_host': parsed.netloc or None,
                'verification_path': parsed.path or None,
            }
            query = parse_qs(parsed.query or '')
            x5secdata = query.get('x5secdata', [None])[0]
            if x5secdata:
                meta['verification_token_hash'] = hashlib.sha256(x5secdata.encode('utf-8')).hexdigest()[:16]
            action = query.get('action', [None])[0]
            if action:
                meta['verification_action'] = action
            step = query.get('x5step', [None])[0]
            if step:
                meta['verification_step'] = step
            return {key: value for key, value in meta.items() if value is not None}
        except Exception as e:
            logger.debug(f"【{self.account_id}】解析验证链接失败: {self._safe_str(e)}")
            return {'verification_source': text[:120]}

    def _build_risk_event_meta(self, trigger_scene: str = None, verification_url: str = None, extra: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        payload: Dict[str, Any] = {}
        if trigger_scene:
            payload['trigger_scene'] = trigger_scene
        payload.update(self._sanitize_verification_meta(verification_url))
        if isinstance(extra, dict):
            payload.update({key: value for key, value in extra.items() if value is not None})
        return payload or None

    def _create_risk_log(
        self,
        event_type: str,
        event_description: str,
        processing_status: str = 'processing',
        processing_result: str = None,
        error_message: str = None,
        session_id: str = None,
        trigger_scene: str = None,
        result_code: str = None,
        event_meta: Optional[Dict[str, Any]] = None,
        duration_ms: Optional[int] = None,
    ) -> Optional[int]:
        try:
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error(
                    "【default】风控日志缺少 canonical account_id，拒绝写入 risk_control_logs"
                )
                return None
            return db_manager.add_risk_control_log(
                account_id=current_account_id,
                event_type=event_type,
                session_id=session_id,
                trigger_scene=trigger_scene,
                result_code=result_code,
                event_description=event_description,
                event_meta=event_meta,
                processing_result=processing_result,
                processing_status=processing_status,
                error_message=error_message,
                duration_ms=duration_ms,
            )
        except Exception as e:
            logger.error(f"【{self.account_id}】记录风控日志失败: {self._safe_str(e)}")
            return None

    def _update_risk_log(
        self,
        log_id: Optional[int],
        *,
        event_description: str = None,
        processing_status: str = None,
        processing_result: str = None,
        error_message: str = None,
        session_id: str = None,
        trigger_scene: str = None,
        result_code: str = None,
        event_meta: Optional[Dict[str, Any]] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        if not log_id:
            return
        try:
            db_manager.update_risk_control_log(
                log_id=log_id,
                event_description=event_description,
                processing_status=processing_status,
                processing_result=processing_result,
                error_message=error_message,
                session_id=session_id,
                trigger_scene=trigger_scene,
                result_code=result_code,
                event_meta=event_meta,
                duration_ms=duration_ms,
            )
        except Exception as e:
            logger.error(f"【{self.account_id}】更新风控日志失败: {self._safe_str(e)}")

    @staticmethod
    def _extract_cookie_value(cookie_info: Optional[Dict[str, Any]]) -> str:
        if not cookie_info:
            return ''
        return (
            cookie_info.get('value')
            or cookie_info.get('cookies_str')
            or cookie_info.get('cookie_value')
            or ''
        )

    def _load_proxy_config(self) -> dict:
        current_account_id = self._canonical_account_id()
        log_account_id = current_account_id or "default"
        try:
            if not current_account_id:
                logger.warning("【default】加载代理配置缺少 canonical account_id，跳过加载代理配置")
                return {
                    'proxy_type': 'none',
                    'proxy_host': '',
                    'proxy_port': 0,
                    'proxy_user': '',
                    'proxy_pass': ''
                }
            proxy_config = db_manager.get_cookie_proxy_config(current_account_id)
            return proxy_config
        except Exception as e:
            logger.warning(f"【{log_account_id}】加载代理配置失败: {e}，使用默认配置（无代理）")
            return {
                'proxy_type': 'none',
                'proxy_host': '',
                'proxy_port': 0,
                'proxy_user': '',
                'proxy_pass': ''
            }

    def _get_proxy_url(self) -> str:
        if not self.proxy_config or self.proxy_config.get('proxy_type', 'none') == 'none':
            return None

        proxy_type = self.proxy_config.get('proxy_type', 'none')
        proxy_host = self.proxy_config.get('proxy_host', '')
        proxy_port = self.proxy_config.get('proxy_port', 0)
        proxy_user = self.proxy_config.get('proxy_user', '')
        proxy_pass = self.proxy_config.get('proxy_pass', '')

        if not proxy_host or not proxy_port:
            return None

        if proxy_user and proxy_pass:
            proxy_url = f"{proxy_type}://{proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}"
        else:
            proxy_url = f"{proxy_type}://{proxy_host}:{proxy_port}"

        return proxy_url

    def _set_connection_state(self, new_state: ConnectionState, reason: str = ""):
        if self.connection_state != new_state:
            old_state = self.connection_state
            self.connection_state = new_state
            self.last_state_change_time = time.time()

            state_msg = f"【{self.account_id}】连接状态: {old_state.value} → {new_state.value}"
            if reason:
                state_msg += f" ({reason})"

            if new_state == ConnectionState.FAILED:
                logger.error(state_msg)
            elif new_state == ConnectionState.RECONNECTING:
                logger.warning(state_msg)
            elif new_state == ConnectionState.CONNECTED:
                logger.success(state_msg)
            else:
                logger.info(state_msg)

    async def _interruptible_sleep(self, duration: float):
        chunk_size = 1.0
        remaining = duration

        while remaining > 0:
            sleep_time = min(chunk_size, remaining)
            try:
                await asyncio.sleep(sleep_time)
                remaining -= sleep_time
            except asyncio.CancelledError:
                raise

    def _reset_stream_activity_state(self, connected_at: Optional[float] = None):
        now = connected_at or time.time()
        self.last_non_heartbeat_message_time = now
        self.last_sync_package_time = 0
        self.last_user_chat_time = 0
        self.last_heartbeat_response = 0
        self.last_sent_heartbeat_mid = None
        self.pending_heartbeat_mids.clear()
        self.last_stream_watchdog_reconnect_time = 0

    def _mark_non_heartbeat_message(self, received_at: Optional[float] = None, *, is_sync_package: bool = False):
        now = received_at or time.time()
        self.last_non_heartbeat_message_time = now
        if is_sync_package:
            self.last_sync_package_time = now
        if self.stream_watchdog_trigger_times:
            self.stream_watchdog_trigger_times.clear()

    async def _force_websocket_reconnect(self, reason: str) -> bool:
        ws = self.ws
        if not ws:
            logger.info(f"【{self.account_id}】{reason}，但当前没有活跃的WebSocket连接")
            return False

        if getattr(ws, "closed", False):
            logger.info(f"【{self.account_id}】{reason}，但当前WebSocket已关闭，等待主循环重连")
            return False

        self._set_connection_state(ConnectionState.RECONNECTING, reason)
        logger.warning(f"【{self.account_id}】{reason}，主动关闭当前WebSocket触发重连")
        try:
            await asyncio.wait_for(ws.close(), timeout=2.0)
            logger.warning(f"【{self.account_id}】当前WebSocket已关闭，主循环将使用最新状态重新连接")
            return True
        except asyncio.TimeoutError:
            logger.warning(f"【{self.account_id}】主动关闭WebSocket超时，等待主循环自行回收连接")
        except Exception as e:
            logger.warning(f"【{self.account_id}】主动关闭WebSocket失败: {self._safe_str(e)}")
        return False

    def _record_message_stream_watchdog_trigger(self, occurred_at: Optional[float] = None) -> int:
        now = occurred_at or time.time()
        window_seconds = max(60, int(self.message_stream_notification_window or 0))
        while self.stream_watchdog_trigger_times and now - self.stream_watchdog_trigger_times[0] > window_seconds:
            self.stream_watchdog_trigger_times.popleft()
        self.stream_watchdog_trigger_times.append(now)
        return len(self.stream_watchdog_trigger_times)

    async def _maybe_notify_message_stream_stale(self, occurred_at: float, connected_for: float, business_idle: float):
        trigger_count = self._record_message_stream_watchdog_trigger(occurred_at)
        if trigger_count < 2:
            return

        window_minutes = max(1, int(self.message_stream_notification_window // 60))
        sync_desc = (
            f"最近同步包距今{(occurred_at - self.last_sync_package_time):.0f}秒"
            if self.last_sync_package_time else
            "当前连接尚未收到同步包"
        )
        user_chat_desc = (
            f"最近真实买家消息距今{(occurred_at - self.last_user_chat_time):.0f}秒"
            if self.last_user_chat_time else
            "当前连接尚未收到真实买家消息"
        )
        notification_message = (
            f"业务消息流疑似假在线，最近{window_minutes}分钟内已连续触发{trigger_count}次自动重连。"
            f"已连接{connected_for:.0f}秒，最近非心跳业务包距今{business_idle:.0f}秒，"
            f"{sync_desc}，{user_chat_desc}"
        )
        await self.send_token_refresh_notification(notification_message, "message_stream_stale")

    async def message_stream_watchdog_loop(self):
        heartbeat_stale_timeout = max(self.heartbeat_timeout * 2, self.heartbeat_interval * 3)
        try:
            while True:
                try:
                    if not self._is_current_account_enabled():
                        logger.info(f"【{self.account_id}】账号已禁用，停止业务流看门狗")
                        break

                    await self._interruptible_sleep(self.stream_watchdog_check_interval)

                    ws = self.ws
                    if not ws or getattr(ws, "closed", False):
                        continue

                    if not self.last_successful_connection:
                        continue

                    now = time.time()
                    connected_for = now - self.last_successful_connection
                    if connected_for < self.stream_watchdog_grace_period:
                        continue

                    if not self.last_heartbeat_response:
                        continue

                    heartbeat_age = now - self.last_heartbeat_response
                    if heartbeat_age > heartbeat_stale_timeout:
                        continue

                    last_business_at = self.last_non_heartbeat_message_time or self.last_successful_connection
                    business_idle = now - last_business_at
                    if business_idle < self.message_stream_watchdog_timeout:
                        continue

                    if (
                        self.last_stream_watchdog_reconnect_time
                        and now - self.last_stream_watchdog_reconnect_time < self.message_stream_watchdog_timeout / 2
                    ):
                        continue

                    self.last_stream_watchdog_reconnect_time = now
                    if self.last_sync_package_time:
                        sync_status = f"最近同步包距今{(now - self.last_sync_package_time):.0f}秒"
                    else:
                        sync_status = "当前连接尚未收到同步包"
                    if self.last_user_chat_time:
                        user_chat_status = f"，最近真实买家消息距今{(now - self.last_user_chat_time):.0f}秒"
                    else:
                        user_chat_status = "，当前连接尚未收到真实买家消息"

                    logger.warning(
                        f"【{self.account_id}】检测到业务流疑似假在线: "
                        f"已连接{connected_for:.0f}秒，最近非心跳业务包距今{business_idle:.0f}秒，{sync_status}{user_chat_status}"
                    )
                    await self._force_websocket_reconnect("业务消息流长时间只有心跳，疑似假在线")
                    await self._maybe_notify_message_stream_stale(now, connected_for, business_idle)
                except asyncio.CancelledError:
                    logger.info(f"【{self.account_id}】业务流看门狗收到取消信号，准备退出")
                    raise
                except Exception as e:
                    logger.error(f"【{self.account_id}】业务流看门狗异常: {self._safe_str(e)}")
                    await self._interruptible_sleep(30)
        except asyncio.CancelledError:
            logger.info(f"【{self.account_id}】业务流看门狗已取消，正在退出...")
            raise
        finally:
            logger.info(f"【{self.account_id}】业务流看门狗已退出")

    def _reset_background_tasks(self):
        logger.info(f"【{self.account_id}】准备重置后台任务引用（仅重置依赖WebSocket的任务）...")

        if self.heartbeat_task:
            status = "已完成" if self.heartbeat_task.done() else "运行中"
            logger.info(f"【{self.account_id}】发现心跳任务（状态: {status}），需要重置（因为依赖WebSocket连接）")
            if not self.heartbeat_task.done():
                try:
                    self.heartbeat_task.cancel()
                    logger.debug(f"【{self.account_id}】已发送取消信号给心跳任务（不等待响应）")
                except Exception as e:
                    logger.warning(f"【{self.account_id}】取消心跳任务失败: {e}")
            self.heartbeat_task = None
            logger.info(f"【{self.account_id}】心跳任务引用已重置")
        else:
            logger.info(f"【{self.account_id}】没有心跳任务需要重置")

        other_tasks_status = []
        if self.token_refresh_task:
            status = "已完成" if self.token_refresh_task.done() else "运行中"
            other_tasks_status.append(f"Token刷新任务({status})")
        if self.cleanup_task:
            status = "已完成" if self.cleanup_task.done() else "运行中"
            other_tasks_status.append(f"清理任务({status})")
        if self.cookie_refresh_task:
            status = "已完成" if self.cookie_refresh_task.done() else "运行中"
            other_tasks_status.append(f"Cookie刷新任务({status})")
        if self.stream_watchdog_task:
            status = "已完成" if self.stream_watchdog_task.done() else "运行中"
            other_tasks_status.append(f"业务流看门狗({status})")

        if other_tasks_status:
            logger.info(f"【{self.account_id}】其他任务继续运行（不依赖WebSocket）: {', '.join(other_tasks_status)}")
        else:
            logger.info(f"【{self.account_id}】没有其他任务在运行")

        logger.info(f"【{self.account_id}】任务重置完成，可以立即创建新的心跳任务")

    async def _cancel_background_tasks(self):
        try:
            tasks_to_cancel = []

            if self.heartbeat_task:
                if not self.heartbeat_task.done():
                    tasks_to_cancel.append(("心跳任务", self.heartbeat_task))
                else:
                    logger.debug(f"【{self.account_id}】心跳任务已完成，跳过")

            if self.token_refresh_task:
                if not self.token_refresh_task.done():
                    tasks_to_cancel.append(("Token刷新任务", self.token_refresh_task))
                else:
                    logger.debug(f"【{self.account_id}】Token刷新任务已完成，跳过")

            if self.cleanup_task:
                if not self.cleanup_task.done():
                    tasks_to_cancel.append(("清理任务", self.cleanup_task))
                else:
                    logger.debug(f"【{self.account_id}】清理任务已完成，跳过")

            if self.cookie_refresh_task:
                if not self.cookie_refresh_task.done():
                    tasks_to_cancel.append(("Cookie刷新任务", self.cookie_refresh_task))
                else:
                    logger.debug(f"【{self.account_id}】Cookie刷新任务已完成，跳过")

            if self.stream_watchdog_task:
                if not self.stream_watchdog_task.done():
                    tasks_to_cancel.append(("业务流看门狗", self.stream_watchdog_task))
                else:
                    logger.debug(f"【{self.account_id}】业务流看门狗已完成，跳过")

            if not tasks_to_cancel:
                logger.info(f"【{self.account_id}】没有后台任务需要取消（所有任务已完成或不存在）")
                self.heartbeat_task = None
                self.token_refresh_task = None
                self.cleanup_task = None
                self.cookie_refresh_task = None
                self.stream_watchdog_task = None
                return

            logger.info(f"【{self.account_id}】开始取消 {len(tasks_to_cancel)} 个未完成的后台任务...")

            for task_name, task in tasks_to_cancel:
                try:
                    if task.done():
                        logger.info(f"【{self.account_id}】任务已完成，跳过取消: {task_name}")
                    else:
                        task.cancel()
                        logger.info(f"【{self.account_id}】已发送取消信号: {task_name}")
                except Exception as e:
                    logger.warning(f"【{self.account_id}】取消任务失败 {task_name}: {e}")

            tasks = [task for _, task in tasks_to_cancel]
            logger.info(f"【{self.account_id}】等待 {len(tasks)} 个任务响应取消信号...")

            wait_timeout = 5.0
            start_time = time.time()
            try:
                pending_tasks_list = [task for task in tasks if not task.done()]

                for task_name, task in tasks_to_cancel:
                    status = "已完成" if task.done() else "运行中"
                    logger.info(f"【{self.account_id}】任务状态: {task_name} - {status}")

                if not pending_tasks_list:
                    logger.info(f"【{self.account_id}】所有任务已完成，无需等待")
                else:
                    logger.info(f"【{self.account_id}】等待 {len(pending_tasks_list)} 个未完成任务响应（超时时间: {wait_timeout}秒）...")
                    try:
                        logger.debug(f"【{self.account_id}】开始调用 asyncio.wait()...")
                        done, pending = await asyncio.wait(
                            pending_tasks_list,
                            timeout=wait_timeout,
                            return_when=asyncio.ALL_COMPLETED
                        )
                        elapsed = time.time() - start_time
                        logger.info(f"【{self.account_id}】asyncio.wait() 返回，耗时 {elapsed:.3f}秒，已完成: {len(done)}，未完成: {len(pending)}")

                        for task_name, task in tasks_to_cancel:
                            if task in done:
                                try:
                                    task.result()
                                    logger.warning(f"【{self.account_id}】⚠️ 任务正常完成（非取消）: {task_name}")
                                except asyncio.CancelledError:
                                    logger.info(f"【{self.account_id}】✅ 任务已成功取消: {task_name}")
                                except Exception as e:
                                    logger.warning(f"【{self.account_id}】⚠️ 任务取消时出现异常 {task_name}: {e}")

                        if pending:
                            pending_names = []
                            for task_name, task in tasks_to_cancel:
                                if task in pending:
                                    pending_names.append(task_name)
                                    if task.done():
                                        try:
                                            task.result()
                                            logger.warning(f"【{self.account_id}】任务在等待期间完成: {task_name}")
                                        except asyncio.CancelledError:
                                            logger.info(f"【{self.account_id}】任务在等待期间被取消: {task_name}")
                                        except Exception as e:
                                            logger.warning(f"【{self.account_id}】任务在等待期间异常 {task_name}: {e}")
                                    else:
                                        logger.warning(f"【{self.account_id}】任务仍未完成: {task_name} (done={task.done()})")

                            logger.warning(f"【{self.account_id}】等待超时 ({elapsed:.3f}秒)，以下任务可能仍在运行: {', '.join(pending_names)}")

                            for task_name, task in tasks_to_cancel:
                                if task in pending and not task.done():
                                    try:
                                        task.cancel()
                                        logger.warning(f"【{self.account_id}】强制取消任务: {task_name}")
                                    except Exception as e:
                                        logger.warning(f"【{self.account_id}】强制取消任务失败 {task_name}: {e}")

                            if pending:
                                try:
                                    done2, pending2 = await asyncio.wait(pending, timeout=1.0, return_when=asyncio.ALL_COMPLETED)
                                    for task_name, task in tasks_to_cancel:
                                        if task in done2:
                                            try:
                                                task.result()
                                            except asyncio.CancelledError:
                                                logger.info(f"【{self.account_id}】任务在二次等待期间被取消: {task_name}")
                                            except Exception as e:
                                                logger.warning(f"【{self.account_id}】任务在二次等待期间异常 {task_name}: {e}")
                                except Exception as e:
                                    logger.warning(f"【{self.account_id}】二次等待任务时出错: {e}")

                            logger.warning(f"【{self.account_id}】强制继续重连流程，未完成的任务将在后台继续运行（但已标记为取消）")
                        else:
                            logger.info(f"【{self.account_id}】所有后台任务已取消 (耗时 {elapsed:.3f}秒)")

                    except Exception as e:
                        elapsed = time.time() - start_time
                        logger.warning(f"【{self.account_id}】等待任务时出错 (耗时 {elapsed:.3f}秒): {e}")
                        import traceback
                        logger.warning(f"【{self.account_id}】等待任务异常堆栈:\n{traceback.format_exc()}")

            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(f"【{self.account_id}】等待任务取消时出错 (耗时 {elapsed:.3f}秒): {e}")
                import traceback
                logger.error(f"【{self.account_id}】等待任务取消异常堆栈:\n{traceback.format_exc()}")

            logger.info(f"【{self.account_id}】任务取消流程完成，继续重连流程")

            for task_name, task in tasks_to_cancel:
                if task and not task.done():
                    logger.warning(f"【{self.account_id}】⚠️ 任务取消流程完成后，任务仍未完成: {task_name} (done={task.done()})")
                elif task and task.done():
                    logger.debug(f"【{self.account_id}】✅ 任务已完成: {task_name}")

        finally:
            self.heartbeat_task = None
            self.token_refresh_task = None
            self.cleanup_task = None
            self.cookie_refresh_task = None
            self.stream_watchdog_task = None
            logger.info(f"【{self.account_id}】后台任务引用已全部重置")

    def _calculate_retry_delay(self, error_msg: str) -> int:
        failure_count = max(
            1,
            int(getattr(self, "connection_failures", 0) or 0),
            int(getattr(self, "init_auth_failures", 0) or 0),
        )

        if "no close frame received or sent" in error_msg:
            return min(3 * failure_count, 15)

        elif "Connection refused" in error_msg or "timeout" in error_msg.lower():
            return min(10 * failure_count, 60)

        else:
            return min(5 * failure_count, 30)

    def _cleanup_instance_caches(self):
        try:
            current_time = time.time()
            cleaned_total = 0

            max_notification_age = 1800
            expired_notifications = [
                key for key, last_time in self.last_notification_time.items()
                if current_time - last_time > max_notification_age
            ]
            for key in expired_notifications:
                del self.last_notification_time[key]
            if expired_notifications:
                cleaned_total += len(expired_notifications)
                logger.warning(f"【{self.account_id}】清理了 {len(expired_notifications)} 个过期知记录")

            max_delivery_age = 1800
            expired_deliveries = [
                delivery_scope_key for delivery_scope_key, last_time in self.last_delivery_time.items()
                if current_time - last_time > max_delivery_age
            ]
            for delivery_scope_key in expired_deliveries:
                del self.last_delivery_time[delivery_scope_key]
                self.delivery_sent_orders.discard(delivery_scope_key)
            if expired_deliveries:
                cleaned_total += len(expired_deliveries)
                logger.warning(f"【{self.account_id}】清理了 {len(expired_deliveries)} 个过期发货记录")

            max_confirm_age = 1800
            expired_confirms = [
                order_id for order_id, last_time in self.confirmed_orders.items()
                if current_time - last_time > max_confirm_age
            ]
            for order_id in expired_confirms:
                del self.confirmed_orders[order_id]
            if expired_confirms:
                cleaned_total += len(expired_confirms)
                logger.warning(f"【{self.account_id}】清理了 {len(expired_confirms)} 个过期订单确认记录")

            if cleaned_total > 0:
                logger.info(f"【{self.account_id}】实例缓存清理完成，共清理 {cleaned_total} 条记录")
                logger.warning(f"【{self.account_id}】当前缓存数量 - 通知: {len(self.last_notification_time)}, 发货: {len(self.last_delivery_time)}, 确认: {len(self.confirmed_orders)}")

        except Exception as e:
            logger.error(f"【{self.account_id}】清理实例缓存时出错: {self._safe_str(e)}")

    async def _cleanup_playwright_cache(self):
        try:
            import shutil
            import glob

            temp_paths = [
                '/tmp/playwright-*',
                '/tmp/chromium-*',
                '/root/.cloakbrowser/*/Cache',
                '/ms-playwright/chromium-*/Default/Cache',
                '/ms-playwright/chromium-*/Default/Code Cache',
                '/ms-playwright/chromium-*/Default/GPUCache',
            ]

            total_cleaned = 0
            total_size_mb = 0

            for pattern in temp_paths:
                try:
                    matching_paths = glob.glob(pattern)
                    for path in matching_paths:
                        try:
                            if os.path.exists(path):
                                if os.path.isdir(path):
                                    size = sum(
                                        os.path.getsize(os.path.join(dirpath, filename))
                                        for dirpath, _, filenames in os.walk(path)
                                        for filename in filenames
                                    )
                                    shutil.rmtree(path, ignore_errors=True)
                                else:
                                    size = os.path.getsize(path)
                                    os.remove(path)

                                total_size_mb += size / (1024 * 1024)
                                total_cleaned += 1
                        except Exception as e:
                            logger.warning(f"清理路径 {path} 时出错: {e}")
                except Exception as e:
                    logger.warning(f"匹配路径 {pattern} 时出错: {e}")

            if total_cleaned > 0:
                logger.info(f"【{self.account_id}】浏览器运行时缓存清理完成: 删除了 {total_cleaned} 个文件/目录，释放 {total_size_mb:.2f} MB")
            else:
                logger.warning(f"【{self.account_id}】浏览器运行时缓存清理: 没有需要清理的临时文件")

        except Exception as e:
            logger.warning(f"【{self.account_id}】清理浏览器运行时缓存时出错: {self._safe_str(e)}")

    async def _cleanup_old_logs(self, retention_days: int = 7):
        try:
            import glob
            from datetime import datetime, timedelta

            logs_dir = "logs"
            if not os.path.exists(logs_dir):
                logger.warning(f"【{self.account_id}】日志目录不存在: {logs_dir}")
                return 0

            cutoff_time = datetime.now() - timedelta(days=retention_days)

            log_patterns = [
                os.path.join(logs_dir, "xianyu_*.log"),
                os.path.join(logs_dir, "xianyu_*.log.zip"),
                os.path.join(logs_dir, "app_*.log"),
                os.path.join(logs_dir, "app_*.log.zip"),
            ]

            total_cleaned = 0
            total_size_mb = 0

            for pattern in log_patterns:
                log_files = glob.glob(pattern)
                for log_file in log_files:
                    try:
                        file_mtime = datetime.fromtimestamp(os.path.getmtime(log_file))

                        if file_mtime < cutoff_time:
                            file_size = os.path.getsize(log_file)
                            os.remove(log_file)
                            total_size_mb += file_size / (1024 * 1024)
                            total_cleaned += 1
                            logger.debug(f"【{self.account_id}】删除过期日志文件: {log_file} (修改时间: {file_mtime})")
                    except Exception as e:
                        logger.warning(f"【{self.account_id}】删除日志文件失败 {log_file}: {self._safe_str(e)}")

            if total_cleaned > 0:
                logger.info(f"【{self.account_id}】日志清理完成: 删除了 {total_cleaned} 个日志文件，释放 {total_size_mb:.2f} MB (保留 {retention_days} 天内的日志)")
            else:
                logger.debug(f"【{self.account_id}】日志清理: 没有需要清理的过期日志文件 (保留 {retention_days} 天)")

            return total_cleaned

        except Exception as e:
            logger.error(f"【{self.account_id}】清理日志文件时出错: {self._safe_str(e)}")
            return 0

    def __init__(
        self,
        cookies_str=None,
        user_id: int = None,
        *,
        account_id: str = None,
        register_instance: bool = True,
    ):
        normalized_account_id = str(account_id or "").strip()
        if normalized_account_id == "default":
            normalized_account_id = ""
        resolved_account_id = self._normalize_account_scope(normalized_account_id)
        init_log_account_id = resolved_account_id or "default"
        logger.info(f"【{init_log_account_id}】开始初始化XianyuLive...")

        if not cookies_str:
            raise ValueError("未提供cookies_str，请显式传入账号 Cookie 内容")

        cookies_str = str(cookies_str).replace("\ufeff", "").strip()

        logger.info(f"【{init_log_account_id}】解析cookies...")
        self.cookies = trans_cookies(cookies_str)
        logger.info(f"【{init_log_account_id}】cookies解析完成，包含字段: {list(self.cookies.keys())}")

        self.account_id = resolved_account_id
        self.cookies_str = cookies_str
        self.user_id = user_id
        self.register_instance = bool(register_instance)
        if self.register_instance and not self._canonical_account_id():
            raise ValueError("register_instance=True requires non-empty account_id")
        self.base_url = WEBSOCKET_URL
        self.websocket_open_timeout = WEBSOCKET_OPEN_TIMEOUT

        if 'unb' not in self.cookies:
            raise ValueError(f"【{init_log_account_id}】Cookie中缺少必需的'unb'字段，当前字段: {list(self.cookies.keys())}")

        self.myid = self.cookies['unb']
        logger.info(f"【{init_log_account_id}】用户ID: {self.myid}")
        self.device_id = generate_device_id(self.myid)

        self.heartbeat_interval = HEARTBEAT_INTERVAL
        self.heartbeat_timeout = HEARTBEAT_TIMEOUT
        self.last_heartbeat_time = 0
        self.last_heartbeat_response = 0
        self.last_sent_heartbeat_mid = None
        self.pending_heartbeat_mids = deque(maxlen=32)
        self.heartbeat_task = None
        self.ws = None
        self.last_non_heartbeat_message_time = 0
        self.last_sync_package_time = 0
        self.last_user_chat_time = 0
        self.last_stream_watchdog_reconnect_time = 0

        self.token_refresh_interval = TOKEN_REFRESH_INTERVAL
        self.token_retry_interval = TOKEN_RETRY_INTERVAL
        self.session_keepalive_interval = SESSION_KEEPALIVE_INTERVAL
        self.session_keepalive_retry_interval = SESSION_KEEPALIVE_RETRY_INTERVAL
        self.last_token_refresh_time = 0
        self.last_session_keepalive_time = 0
        self.current_token = None
        self.token_refresh_task = None
        self.last_token_refresh_status = None
        self.last_token_refresh_error_message = None
        self.last_session_keepalive_status = None
        self.last_session_keepalive_error_message = None
        self.pending_slider_success_notice = None
        self.connection_restart_flag = False
        self.last_init_failure_reason = None
        self.last_init_failure_type = None
        self.init_auth_failures = 0
        self.stream_watchdog_task = None
        self.stream_watchdog_check_interval = max(self.heartbeat_interval, 15)

        self.stream_watchdog_grace_period = max(self.heartbeat_interval * 4, 120)
        self.message_stream_watchdog_timeout = max(self.session_keepalive_interval * 3, 1800)
        self.stream_watchdog_trigger_times = deque(maxlen=8)
        self.message_stream_notification_window = max(self.message_stream_watchdog_timeout * 2, 3600)
        self.message_stream_notification_cooldown = max(self.message_stream_watchdog_timeout, 1800)

        canonical_account_id = self._canonical_account_id()
        prewarmed_token_info = self.pop_auth_prewarmed_token(canonical_account_id)
        if prewarmed_token_info:
            self.current_token = prewarmed_token_info.get('token')
            self.last_token_refresh_time = prewarmed_token_info.get('timestamp', time.time())
            logger.info(
                f"【{init_log_account_id}】已复用认证预热token，来源: {prewarmed_token_info.get('source') or 'unknown'}"
            )

        prewarmed_token_info = self.pop_qr_prewarmed_token(canonical_account_id)
        if prewarmed_token_info and not self.current_token:
            self.current_token = prewarmed_token_info.get('token')
            self.last_token_refresh_time = prewarmed_token_info.get('timestamp', time.time())
            logger.info(f"【{init_log_account_id}】已复用扫码预热token，跳过首次token刷新")

        self.last_notification_time = {}
        self.notification_cooldown = 300
        self.token_refresh_notification_cooldown = 18000
        self.notification_lock = asyncio.Lock()
        self.pending_notification_keys = set()
        self.last_delivery_time = {}
        self.delivery_cooldown = 600

        self.confirmed_orders = {}
        self.order_confirm_cooldown = 600

        self.delivery_sent_orders = set()
        self.session = None

        self.proxy_config = self._load_proxy_config()
        if self.proxy_config.get('proxy_type', 'none') != 'none':
            logger.info(f"【{init_log_account_id}】已加载代理配置: {self.proxy_config['proxy_type']}://{self.proxy_config['proxy_host']}:{self.proxy_config['proxy_port']}")

        self.cleanup_task = None

        self.cookie_refresh_task = None
        self.cookie_refresh_interval = 10800
        self.last_cookie_refresh_time = 0
        self.cookie_refresh_lock = asyncio.Lock()
        self.cookie_refresh_enabled = True

        self.last_qr_cookie_refresh_time = 0
        self.qr_cookie_refresh_cooldown = 600

        self.last_message_received_time = 0
        self.message_cookie_refresh_cooldown = 300

        self.browser_cookie_refreshed = False
        self.restarted_in_browser_refresh = False

        self.captcha_verification_count = 0
        self.max_captcha_verification_count = 3
        self.last_slider_success_at = 0.0
        self.last_slider_success_cookie_length = 0
        self.slider_success_reentry_window = 30
        self.post_slider_token_retry_delay = (1.5, 3.0)
        self.token_refresh_lock = asyncio.Lock()

        self.connection_state = ConnectionState.DISCONNECTED
        self.connection_failures = 0
        self.max_connection_failures = 5
        self.last_successful_connection = 0
        self.last_state_change_time = time.time()
        self.background_tasks = set()
        self.message_semaphore = asyncio.Semaphore(100)
        self.active_message_tasks = 0

        self.message_queue_enabled = True
        self.message_queue_max_size = 1000
        self.message_queue_workers = 5
        self.message_expire_seconds = 60

        self.message_queue = asyncio.PriorityQueue(maxsize=self.message_queue_max_size)
        self.message_queue_counter = 0
        self.message_queue_lock = asyncio.Lock()

        self.message_workers = []
        self.message_queue_running = False
        self.queue_stats = {
            'received': 0,
            'processed': 0,
            'dropped_full': 0,
            'dropped_expired': 0,
            'errors': 0,
            'last_stats_time': time.time(),
        }

        self.yifan_account_waiting = {}
        self.yifan_account_lock = asyncio.Lock()

        self.message_debounce_tasks = {}
        self._message_debounce_delay = 3
        self.message_debounce_lock = asyncio.Lock()

        self.processed_message_ids = {}
        self.pending_message_ids = {}
        self.processed_message_ids_lock = asyncio.Lock()
        self.processed_message_ids_max_size = 10000
        self.message_expire_time = 3600
        self.pending_message_expire_time = 300
        self.auto_reply_send_retry_delays = (1, 3)
        self.order_detail_retry_tasks = {}
        self.order_detail_force_refresh_marks = {}
        self.order_detail_force_refresh_cooldown = 5

        self._init_order_status_handler()

        if self.register_instance:
            self._register_instance()

    def _current_account_id(self) -> str:
        return self._canonical_account_id()

    def _canonical_account_id(self) -> str:
        account_id = str(getattr(self, "account_id", None) or "").strip()
        if account_id == "default":
            return ""
        return account_id

    @classmethod
    def _compose_order_detail_scope_key(cls, account_id: Any, order_id: Any):
        normalized_account_id = cls._normalize_account_scope(account_id)
        normalized_order_id = str(order_id or "").strip()
        if not normalized_account_id or not normalized_order_id:
            return None
        return normalized_account_id, normalized_order_id

    @classmethod
    def _compose_order_delivery_scope_key(cls, account_id: Any, order_id: Any):
        normalized_account_id = cls._normalize_account_scope(account_id)
        normalized_order_id = str(order_id or "").strip()
        if not normalized_account_id or normalized_account_id == "default" or not normalized_order_id:
            return None
        return normalized_account_id, normalized_order_id

    @property
    def message_debounce_delay(self) -> int:
        try:
            from db_manager import db_manager
            val = db_manager.get_system_setting('message_debounce_delay')
            return int(val) if val else self._message_debounce_delay
        except Exception:
            return self._message_debounce_delay

    def _is_current_account_enabled(self) -> bool:
        current_account_id = self._canonical_account_id()
        if not current_account_id:
            return True

        try:
            from cookie_manager import manager as cookie_manager
        except Exception:
            return True

        try:
            if cookie_manager is None:
                return True
            return bool(cookie_manager.get_cookie_status(current_account_id))
        except Exception:
            return True

    def _init_order_status_handler(self):
        try:
            from order_status_handler import order_status_handler
            self.order_status_handler = order_status_handler
            logger.info(f"【{self.account_id}】订单状态处理器已启用")
        except Exception as e:
            logger.error(f"【{self.account_id}】初始化订单状处理器失败: {self._safe_str(e)}")
            self.order_status_handler = None

    def _register_instance(self):
        try:
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.warning("【missing-account-id】实例缺少 account_id，跳过全局注册")
                return
            XianyuLive._instances[current_account_id] = self
            logger.warning(f"【{current_account_id}】实例已注册到全局字典")
        except Exception as e:
            logger.error(f"【{self.account_id}】注册实例失败: {self._safe_str(e)}")

    def _unregister_instance(self):
        try:
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                return
            if current_account_id in XianyuLive._instances:
                del XianyuLive._instances[current_account_id]
                logger.warning(f"【{current_account_id}】实例已从全局字典中注销")
        except Exception as e:
            logger.error(f"【{self.account_id}】注销实例失败: {self._safe_str(e)}")

    @classmethod
    def get_instance(cls, account_id: str = None):
        resolved_account_id = cls._normalize_account_scope(account_id)
        return cls._instances.get(resolved_account_id)

    @classmethod
    def get_all_instances(cls):
        return dict(cls._instances)

    @classmethod
    def get_instance_count(cls):
        return len(cls._instances)

    @classmethod
    def is_manual_refresh_active(
        cls,
        account_id: str = None,
        allow_handoff_recovery: bool = False,
    ) -> bool:
        resolved_account_id = cls._normalize_manual_refresh_account_scope(account_id)
        if not resolved_account_id:
            return False
        state = cls.get_manual_refresh_state(account_id=resolved_account_id)
        if not state:
            return False
        phase = state.get('phase') or 'manual_refresh'
        if allow_handoff_recovery and phase == 'handoff_recovery':
            return False
        return True

    @classmethod
    def begin_manual_refresh(
        cls,
        account_id: str = None,
        source: str = "manual_refresh",
    ) -> Dict[str, Any]:
        resolved_account_id = cls._normalize_manual_refresh_account_scope(account_id)
        if not resolved_account_id:
            return {"started": False, "already_active": False, "reason": "empty_account_id"}

        live_instance = cls.get_instance(account_id=resolved_account_id)
        previous_cookie_refresh_enabled = None
        if live_instance is not None:
            previous_cookie_refresh_enabled = live_instance.cookie_refresh_enabled

        cls._cleanup_manual_refresh_state()
        with cls._manual_refresh_lock:
            existing = cls._manual_refresh_state.get(resolved_account_id)
            if existing:
                existing["source"] = source
                existing["phase"] = 'manual_refresh'
                existing["updated_at"] = time.time()
                existing["expires_at"] = None
                return {
                    "started": False,
                    "already_active": True,
                    "previous_cookie_refresh_enabled": existing.get("previous_cookie_refresh_enabled")
                }

            cls._manual_refresh_state[resolved_account_id] = {
                "source": source,
                "phase": 'manual_refresh',
                "started_at": time.time(),
                "updated_at": time.time(),
                "expires_at": None,
                "previous_cookie_refresh_enabled": previous_cookie_refresh_enabled,
            }

        if live_instance is not None and previous_cookie_refresh_enabled is not None:
            live_instance.enable_cookie_refresh(False)
            logger.warning(f"【{resolved_account_id}】已进入手动刷新保护期，暂停自动Cookie刷新")
        else:
            logger.warning(f"【{resolved_account_id}】已进入手动刷新保护期，当前无运行中的账号实例")

        return {
            "started": True,
            "already_active": False,
            "previous_cookie_refresh_enabled": previous_cookie_refresh_enabled
        }

    @classmethod
    def end_manual_refresh(
        cls,
        account_id: str = None,
        source: str = "manual_refresh",
    ) -> bool:
        resolved_account_id = cls._normalize_manual_refresh_account_scope(account_id)
        if not resolved_account_id:
            return False

        cls._cleanup_manual_refresh_state()
        with cls._manual_refresh_lock:
            state = cls._manual_refresh_state.pop(resolved_account_id, None)

        if state is None:
            return False

        live_instance = cls.get_instance(account_id=resolved_account_id)
        previous_cookie_refresh_enabled = state.get("previous_cookie_refresh_enabled")
        if live_instance is not None and previous_cookie_refresh_enabled is not None:
            live_instance.enable_cookie_refresh(previous_cookie_refresh_enabled)
            if previous_cookie_refresh_enabled:
                live_instance.last_cookie_refresh_time = time.time()
            logger.warning(
                f"【{resolved_account_id}】手动刷新保护期已结束，恢复自动Cookie刷新: {previous_cookie_refresh_enabled}"
            )
        else:
            logger.warning(f"【{resolved_account_id}】手动刷新保护期已结束，当前无运行中的账号实例可恢复")

        logger.info(f"【{resolved_account_id}】结束手动刷新保护期，来源: {source}")
        return True

    def _create_tracked_task(self, coro):
        task = asyncio.create_task(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    def _sanitize_buyer_nick(self, candidate: Any, *, source: str = "unknown",
                             message_meta: Dict[str, Any] = None, log_prefix: str = "") -> Optional[str]:
        if candidate is None:
            return None

        text = str(candidate).strip()
        if not text or text in {"未知用户", "unknown", "unknown_user"}:
            return None

        invalid_exact_titles = {
            "订单",
            "全部",
            "交易消息",
            "等待你发货",
            "你人真不错，送你闲鱼小红花",
            "卖家人不错？送Ta闲鱼小红花",
            "快给ta一个评价吧～",
        }
        if text in invalid_exact_titles:
            logger.info(f"{log_prefix} 👤 忽略系统标题型买家昵称({source}): {text}")
            return None

        meta = message_meta if isinstance(message_meta, dict) else {}
        related_notice_texts = []
        for key in ("detailNotice", "reminderContent", "reminderNotice"):
            value = str(meta.get(key, "")).strip()
            if value:
                related_notice_texts.append(value)

        if text in related_notice_texts:
            logger.info(f"{log_prefix} 👤 忽略通知文案型买家昵称({source}): {text}")
            return None

        reminder_title = str(meta.get("reminderTitle", "")).strip()
        if source != "senderNick":
            invalid_keywords = (
                "小红花", "待付款", "待发货", "待刀成", "成功小刀", "闲鱼",
                "交易", "收货", "退款", "评价", "发货", "付款", "拍下",
                "确认", "关闭", "鼓励", "真不错", "全部", "订单",
            )
            if any(keyword in text for keyword in invalid_keywords):
                logger.info(f"{log_prefix} 👤 忽略系统关键词型买家昵称({source}): {text}")
                return None

            if reminder_title == text and len(text) >= 10 and any(ch in text for ch in "?。！?!?～~"):
                logger.info(f"{log_prefix} 👤 忽略长句型买家昵称({source}): {text}")
                return None

        return text

    def _resolve_delivery_log_buyer_nick(self, buyer_nick: Any = None, *, order_id: str = None,
                                         buyer_id: str = None, log_prefix: str = "") -> Optional[str]:
        from db_manager import db_manager

        canonical_account_id = self._canonical_account_id()
        normalized_order_id = str(order_id).strip() if order_id else None
        normalized_buyer_id = self._normalize_buyer_id_value(buyer_id)

        if not canonical_account_id:
            logger.error(
                f"{log_prefix} 发货日志买家昵称解析缺少 canonical account_id，拒绝继续运行"
            )
            return self._sanitize_buyer_nick(
                buyer_nick,
                source="delivery_log_raw",
                log_prefix=log_prefix,
            )

        try:
            if normalized_order_id:
                order_info = db_manager.get_order_by_id(
                    normalized_order_id,
                    account_id=canonical_account_id,
                )
                if order_info:
                    order_account_id = str(order_info.get("account_id") or "").strip()
                    if order_account_id and order_account_id == canonical_account_id:
                        order_buyer_nick = self._sanitize_buyer_nick(
                            order_info.get("buyer_nick"),
                            source="delivery_log_order",
                            log_prefix=log_prefix,
                        )
                        if order_buyer_nick:
                            return order_buyer_nick

                    if not normalized_buyer_id:
                        normalized_buyer_id = self._normalize_buyer_id_value(
                            order_info.get("buyer_id")
                        )

            if normalized_buyer_id:
                recent_order = db_manager.get_recent_order_by_buyer_id(
                    normalized_buyer_id,
                    account_id=canonical_account_id,
                    minutes=60,
                )
                if recent_order:
                    recent_buyer_nick = self._sanitize_buyer_nick(
                        recent_order.get("buyer_nick"),
                        source="delivery_log_recent_order",
                        log_prefix=log_prefix,
                    )
                    if recent_buyer_nick:
                        return recent_buyer_nick
        except Exception as resolve_error:
            logger.warning(
                f"{log_prefix} 发货日志买家昵称解析失败: {self._safe_str(resolve_error)}"
            )

        return self._sanitize_buyer_nick(
            buyer_nick,
            source="delivery_log_raw",
            log_prefix=log_prefix,
        )

    def _select_delivery_order_sid_candidate(self, orders: list, *, sid: str,
                                             branch_label: str, item_id: str = None,
                                             buyer_id: str = None, log_prefix: str = ""):
        if not orders:
            return None, None
        if len(orders) == 1:
            return orders[0], "direct"

        normalized_item_id = str(item_id or "").strip()
        if normalized_item_id == "未知商品":
            normalized_item_id = ""
        normalized_buyer_id = self._normalize_buyer_id_value(buyer_id)
        if normalized_buyer_id and not self._is_trustworthy_buyer_id(normalized_buyer_id):
            normalized_buyer_id = None

        def _match_item(order: dict) -> bool:
            return normalized_item_id and str(order.get("item_id") or "").strip() == normalized_item_id

        def _match_buyer(order: dict) -> bool:
            return normalized_buyer_id and self._normalize_buyer_id_value(order.get("buyer_id")) == normalized_buyer_id

        candidate_filters = []
        if normalized_item_id and normalized_buyer_id:
            candidate_filters.append(
                (
                    "sid_item_buyer",
                    lambda order: _match_item(order) and _match_buyer(order),
                )
            )
        if normalized_item_id:
            candidate_filters.append(("sid_item", _match_item))
        if normalized_buyer_id:
            candidate_filters.append(("sid_buyer", _match_buyer))

        for selection_mode, matcher in candidate_filters:
            matched_orders = [order for order in orders if matcher(order)]
            if len(matched_orders) == 1:
                return matched_orders[0], selection_mode
            if len(matched_orders) > 1:
                logger.warning(
                    f"{log_prefix} sid兜底命中多个候选，拒绝盲选: "
                    f"sid={sid}, branch={branch_label}, mode={selection_mode}, "
                    f"candidates={len(matched_orders)}, item_id={normalized_item_id or '-'}, "
                    f"buyer_id={normalized_buyer_id or '-'}"
                )
                return None, f"ambiguous_{branch_label}"

        logger.warning(
            f"{log_prefix} sid兜底命中多个候选，缺少唯一匹配上下文，拒绝盲选: "
            f"sid={sid}, branch={branch_label}, candidates={len(orders)}, "
            f"item_id={normalized_item_id or '-'}, buyer_id={normalized_buyer_id or '-'}"
        )
        return None, f"ambiguous_{branch_label}"

    def _lookup_delivery_order_by_sid(self, sid: str, *, item_id: str = None,
                                      buyer_id: str = None, minutes: int = 10,
                                      log_prefix: str = "") -> Dict[str, Any]:
        canonical_account_id = self._canonical_account_id()
        normalized_sid = str(sid or "").strip()
        if not normalized_sid:
            return {"match_type": "missing", "order": None}
        if not canonical_account_id:
            logger.error(
                f"{log_prefix} sid兜底查单缺少 canonical account_id，拒绝继续运行"
            )
            return {"match_type": "missing", "order": None}

        try:
            pending_orders = db_manager.find_recent_orders_by_match_context(
                sid=normalized_sid,
                account_id=canonical_account_id,
                statuses=[
                    "pending_ship",
                    "pending_delivery",
                    "partial_success",
                    "partial_pending_finalize",
                ],
                minutes=minutes,
                limit=5,
            )
        except Exception as lookup_error:
            logger.error(f"{log_prefix} sid兜底查单异常: {self._safe_str(lookup_error)}")
            return {"match_type": "error", "order": None}

        if pending_orders:
            order, selection_mode = self._select_delivery_order_sid_candidate(
                pending_orders,
                sid=normalized_sid,
                branch_label="pending_ship",
                item_id=item_id,
                buyer_id=buyer_id,
                log_prefix=log_prefix,
            )
            if not order:
                return {"match_type": selection_mode or "ambiguous_pending_ship", "order": None}
            logger.info(
                f"{log_prefix} sid兜底命中待发货订单: sid={normalized_sid}, "
                f"order_id={order.get('order_id')}, status={order.get('order_status') or 'unknown'}, "
                f"mode={selection_mode or 'direct'}"
            )
            return {"match_type": "pending_ship", "order": order}

        try:
            recent_orders = db_manager.find_recent_orders_by_match_context(
                sid=normalized_sid,
                account_id=canonical_account_id,
                statuses=[
                    "processing",
                    "pending_payment",
                    "shipped",
                    "completed",
                    "cancelled",
                ],
                minutes=minutes,
                limit=5,
            )
        except Exception as lookup_error:
            logger.error(f"{log_prefix} sid兜底查单异常: {self._safe_str(lookup_error)}")
            return {"match_type": "error", "order": None}

        if not recent_orders:
            return {"match_type": "missing", "order": None}

        order, selection_mode = self._select_delivery_order_sid_candidate(
            recent_orders,
            sid=normalized_sid,
            branch_label="recent",
            item_id=item_id,
            buyer_id=buyer_id,
            log_prefix=log_prefix,
        )
        if not order:
            return {"match_type": selection_mode or "ambiguous_recent", "order": None}

        order_id = str(order.get("order_id") or "").strip()
        order_status = str(order.get("order_status") or "").strip()
        if order_status == "shipped":
            if self._has_delivery_progress_evidence(order_id):
                match_type = "already_processed"
            else:
                match_type = "suspicious_shipped"
                logger.warning(
                    f"{log_prefix} sid兜底命中可疑已发货订单，检测到无真实发货进度，继续允许纠偏: "
                    f"sid={normalized_sid}, order_id={order_id}, status={order_status}"
                )
        elif order_status == "completed":
            match_type = "already_processed"
        elif order_status == "cancelled":
            match_type = "cancelled"
        elif order_status in {"processing", "pending_payment"}:
            match_type = "not_ready"
        else:
            match_type = "other_status"

        logger.info(
            f"{log_prefix} sid兜底命中订单: sid={normalized_sid}, "
            f"order_id={order.get('order_id')}, status={order_status or 'unknown'}, "
            f"match_type={match_type}, mode={selection_mode or 'direct'}"
        )
        return {"match_type": match_type, "order": order}

    async def _refresh_sid_lookup_if_needed(self, sid: str, sid_lookup: Dict[str, Any], *,
                                            item_id: str = None, buyer_id: str = None,
                                            minutes: int = 10, allow_bargain_ready: bool = False,
                                            log_prefix: str = "") -> Dict[str, Any]:
        recent_order = (sid_lookup or {}).get('order')
        match_type = (sid_lookup or {}).get('match_type', 'missing')

        if not recent_order or match_type not in {'not_ready', 'other_status', 'suspicious_shipped'}:
            return sid_lookup

        order_id = str(recent_order.get('order_id') or '').strip()
        if not order_id:
            return sid_lookup

        refresh_item_id = recent_order.get('item_id') or item_id
        refresh_buyer_id = recent_order.get('buyer_id') or buyer_id
        old_status = recent_order.get('order_status') or 'unknown'

        logger.info(
            f"{log_prefix} sid命中的订单状态未就绪，尝试强制刷新订单详情后重试: "
            f"order_id={order_id}, status={old_status}"
        )

        if not self._reserve_order_detail_force_refresh(
            order_id,
            reason='sid_not_ready',
            log_prefix=log_prefix,
        ):
            return sid_lookup

        try:
            await self.fetch_order_detail_info(
                order_id,
                refresh_item_id,
                refresh_buyer_id,
                sid=sid,
                force_refresh=True
            )
        except Exception as refresh_error:
            logger.warning(f"{log_prefix} sid未就绪订单强刷失败: {self._safe_str(refresh_error)}")
            return sid_lookup

        refreshed_lookup = self._lookup_delivery_order_by_sid(
            sid,
            item_id=refresh_item_id,
            buyer_id=refresh_buyer_id,
            minutes=minutes,
            log_prefix=log_prefix
        )
        refreshed_order = refreshed_lookup.get('order') or {}

        if (
            allow_bargain_ready and
            refreshed_lookup.get('match_type') == 'not_ready' and
            refreshed_order and
            str(refreshed_order.get('order_status') or '').strip() in {'processing', 'pending_payment'} and
            self._has_bargain_success_evidence(refreshed_order)
        ):
            refreshed_lookup = dict(refreshed_lookup)
            refreshed_lookup['match_type'] = 'bargain_ready'
            logger.info(
                f"{log_prefix} sid强刷后仍未进入待发货，但检测到小刀成功证据，"
                f"改用小刀兜底发货: order_id={refreshed_order.get('order_id') or order_id}, "
                f"status={refreshed_order.get('order_status') or 'unknown'}"
            )

        logger.info(
            f"{log_prefix} sid强刷后重新判定: order_id={refreshed_order.get('order_id') or order_id}, "
            f"status={refreshed_order.get('order_status') or 'unknown'}, "
            f"match_type={refreshed_lookup.get('match_type', 'missing')}"
        )
        return refreshed_lookup

    async def _ensure_item_owned_by_current_account(self, item_id: str, *,
                                                    log_prefix: str = "",
                                                    page_size: int = 50,
                                                    max_pages: int = 3) -> bool:
        canonical_account_id = self._canonical_account_id()
        if not item_id or item_id == "未知商品":
            return False
        if not canonical_account_id:
            logger.error(
                f"{log_prefix} 商品归属校验缺少 canonical account_id，拒绝继续运行: item_id={item_id}"
            )
            return False

        existing_item = db_manager.get_item_info(canonical_account_id, item_id)
        if existing_item:
            return True

        logger.info(f"{log_prefix} 商品 {item_id} 未命中本地缓存，刷新在售商品列表后重试归属校验")
        try:
            for page_number in range(1, max_pages + 1):
                result = await self.get_item_list_info(page_number=page_number, page_size=page_size)
                if not result.get("success"):
                    logger.warning(f"{log_prefix} 刷新在售商品列表失败，停止归属校验回退: page={page_number}, result={result}")
                    break

                current_items = result.get("items", [])
                if any(str(item.get("id", "")).strip() == str(item_id).strip() for item in current_items):
                    logger.info(f"{log_prefix} 商品 {item_id} 在第 {page_number} 页在售商品列表中命中，归属校验过")
                    return True

                if len(current_items) < page_size:
                    break
        except Exception as e:
            logger.error(f"{log_prefix} 刷新在售商品列表进行归属校验失败: {self._safe_str(e)}")

        return bool(db_manager.get_item_info(canonical_account_id, item_id))

    _INVALID_BUYER_IDS = {"unknown_user", "unknown", "", "None", "null", "0", "-", "-1"}

    @classmethod
    def _normalize_buyer_id_value(cls, buyer_id) -> Optional[str]:
        if buyer_id is None:
            return None
        text = str(buyer_id).strip()
        if not text:
            return None
        if text.endswith('@goofish'):
            text = text.split('@')[0].strip()
        return text or None

    @staticmethod
    def _is_trustworthy_buyer_id(buyer_id) -> bool:
        normalized_buyer_id = XianyuLive._normalize_buyer_id_value(buyer_id)
        if not normalized_buyer_id:
            return False
        if normalized_buyer_id in XianyuLive._INVALID_BUYER_IDS:
            return False
        if normalized_buyer_id.isdigit() and len(normalized_buyer_id) <= 2:
            return False
        return True

    def _extract_query_value_from_url(self, url_text: Any, key: str) -> Optional[str]:
        text = str(url_text or '').strip()
        if not text:
            return None

        try:
            parsed = urlparse(text)
            query = parse_qs(parsed.query or '')
            value = query.get(key, [None])[0]
            return self._normalize_buyer_id_value(value)
        except Exception as e:
            logger.debug(f"【{self.account_id}】解析链接参数失败: key={key}, error={self._safe_str(e)}")
            return None

    def _extract_buyer_id_from_message_meta(self, message_meta: dict, *, meta_label: str,
                                            log_prefix: str = "") -> Tuple[Optional[str], Optional[str]]:
        if not isinstance(message_meta, dict):
            return None, None

        biz_tag_dict = self._load_json_dict(message_meta.get('bizTag', ''))
        candidates = [
            ('reminderUrl.peerUserId', self._extract_query_value_from_url(message_meta.get('reminderUrl'), 'peerUserId')),
            ('bizTag.senderId', self._normalize_buyer_id_value(biz_tag_dict.get('senderId') or biz_tag_dict.get('sender_id'))),
            (f'{meta_label}.senderUserId', self._normalize_buyer_id_value(message_meta.get('senderUserId'))),
        ]

        low_trust_candidates = []
        for source, candidate in candidates:
            if not candidate:
                continue
            if self._is_trustworthy_buyer_id(candidate):
                return candidate, source
            low_trust_candidates.append(f'{source}={candidate}')

        if low_trust_candidates:
            logger.info(
                f"{log_prefix} 👤 检测到低可信买家ID候选，已忽略: {', '.join(low_trust_candidates[:3])}"
            )
        return None, None

    def _select_buyer_identity_for_order_write(self, order_id: str, *, incoming_buyer_id: Any = None,
                                               incoming_buyer_nick: Any = None, existing_order: Dict[str, Any] = None,
                                               buyer_id_source: str = None, buyer_nick_source: str = 'unknown',
                                               log_prefix: str = '') -> Tuple[Optional[str], Optional[str], bool]:
        incoming_buyer_id = self._normalize_buyer_id_value(incoming_buyer_id)
        incoming_buyer_nick = self._sanitize_buyer_nick(
            incoming_buyer_nick,
            source=buyer_nick_source,
            log_prefix=log_prefix,
        )

        existing_buyer_id = self._normalize_buyer_id_value((existing_order or {}).get('buyer_id'))
        existing_buyer_nick = (existing_order or {}).get('buyer_nick')
        existing_buyer_is_trustworthy = self._is_trustworthy_buyer_id(existing_buyer_id)
        incoming_buyer_is_trustworthy = self._is_trustworthy_buyer_id(incoming_buyer_id)
        source_label = buyer_id_source or 'unknown'

        if incoming_buyer_id and incoming_buyer_id == self.myid:
            if existing_order:
                preserved_buyer_id = existing_buyer_id if existing_buyer_id and existing_buyer_id != self.myid else None
                if existing_buyer_nick:
                    incoming_buyer_nick = existing_buyer_nick
                logger.info(
                    f"{log_prefix} 订单 {order_id} 命中自己买家ID保护，继续刷新并保留已有买家信息: "
                    f"incoming_buyer_id={incoming_buyer_id}, preserved_buyer_id={preserved_buyer_id}"
                )
                return preserved_buyer_id, incoming_buyer_nick, False

            logger.info(
                f"{log_prefix} 跳过疑似买家订单 {order_id} 的首次写入，buyer_id={incoming_buyer_id} 等于自己的ID"
            )
            return None, incoming_buyer_nick, True

        if existing_buyer_is_trustworthy:
            if not incoming_buyer_id:
                return existing_buyer_id, incoming_buyer_nick or existing_buyer_nick, False

            if not incoming_buyer_is_trustworthy:
                logger.info(
                    f"{log_prefix} 忽略低可信buyer_id覆盖，保留已有买家信息: "
                    f"order_id={order_id}, incoming_buyer_id={incoming_buyer_id}, "
                    f"incoming_source={source_label}, preserved_buyer_id={existing_buyer_id}"
                )
                return existing_buyer_id, incoming_buyer_nick or existing_buyer_nick, False

            if incoming_buyer_id != existing_buyer_id:
                logger.warning(
                    f"{log_prefix} 检测到买家ID冲突，保留已有可信买家信息: "
                    f"order_id={order_id}, incoming_buyer_id={incoming_buyer_id}, "
                    f"incoming_source={source_label}, preserved_buyer_id={existing_buyer_id}"
                )
                return existing_buyer_id, incoming_buyer_nick or existing_buyer_nick, False

            return existing_buyer_id, incoming_buyer_nick or existing_buyer_nick, False

        if incoming_buyer_is_trustworthy:
            return incoming_buyer_id, incoming_buyer_nick or existing_buyer_nick, False

        if incoming_buyer_id:
            logger.info(
                f"{log_prefix} 检测到低可信buyer_id，暂不写入订单: "
                f"order_id={order_id}, incoming_buyer_id={incoming_buyer_id}, incoming_source={source_label}"
            )

        fallback_buyer_id = existing_buyer_id if existing_buyer_id and existing_buyer_id != self.myid else None
        return fallback_buyer_id, incoming_buyer_nick or existing_buyer_nick, False

    def _extract_order_message_context(self, message: dict, msg_id: str = None) -> Dict[str, Any]:
        buyer_id = None
        buyer_id_source = None
        buyer_nick = None
        sid = ""
        item_id = None
        log_prefix = f"【{self.account_id}】[{msg_id}]" if msg_id else f"【{self.account_id}?"

        try:
            message_1 = message.get("1")
            if isinstance(message_1, str):
                if '@' in message_1:
                    sid = message_1
                else:
                    sid = message.get("2", "") or ""
                buyer_id = None
                message_4 = message.get("4")
                if isinstance(message_4, dict):
                    buyer_id, buyer_id_source = self._extract_buyer_id_from_message_meta(
                        message_4,
                        meta_label='message[4]',
                        log_prefix=log_prefix,
                    )
                    buyer_nick = self._sanitize_buyer_nick(
                        message_4.get("senderNick"),
                        source="senderNick(msg4)",
                        message_meta=message_4,
                        log_prefix=log_prefix
                    )
                    if not buyer_nick:
                        reminder_title = message_4.get("reminderTitle", "")
                        buyer_nick = self._sanitize_buyer_nick(
                            reminder_title,
                            source="reminderTitle(msg4)",
                            message_meta=message_4,
                            log_prefix=log_prefix
                        )
                        if buyer_nick:
                            logger.info(f"{log_prefix} 👤 从message[4].reminderTitle提取到买家昵称: {buyer_nick}")
                    if buyer_nick:
                        logger.info(f"{log_prefix} 👤 从message[4]提取到买家昵称: {buyer_nick}")
                logger.info(
                    f"{log_prefix} 📌 简化消息，sid: {sid}，buyer_id: {buyer_id}，"
                    f"buyer_id_source: {buyer_id_source or '-'}"
                )
            elif isinstance(message_1, dict):
                if "10" in message_1 and isinstance(message_1["10"], dict):
                    message_10 = message_1["10"]
                    buyer_id, buyer_id_source = self._extract_buyer_id_from_message_meta(
                        message_10,
                        meta_label='message[1][10]',
                        log_prefix=log_prefix,
                    )
                    buyer_nick = self._sanitize_buyer_nick(
                        message_10.get("senderNick"),
                        source="senderNick",
                        message_meta=message_10,
                        log_prefix=log_prefix
                    )
                    if not buyer_nick:
                        reminder_title = message_10.get("reminderTitle", "")
                        buyer_nick = self._sanitize_buyer_nick(
                            reminder_title,
                            source="reminderTitle",
                            message_meta=message_10,
                            log_prefix=log_prefix
                        )
                        if buyer_nick:
                            logger.info(f"{log_prefix} 👤 从reminderTitle提取到买家昵称: {buyer_nick}")
                    if buyer_nick:
                        logger.info(f"{log_prefix} 👤 提取到买家昵称: {buyer_nick}")
                sid = message_1.get("2", "")
                if sid:
                    logger.info(f"{log_prefix} 📌 提取到sid: {sid}")
        except Exception as context_e:
            logger.warning(f"{log_prefix} 提取订单上下文失败: {self._safe_str(context_e)}")

        try:
            if "1" in message and isinstance(message["1"], dict) and "10" in message["1"] and isinstance(message["1"]["10"], dict):
                url_info = message["1"]["10"].get("reminderUrl", "")
                if isinstance(url_info, str) and "itemId=" in url_info:
                    item_id = url_info.split("itemId=")[1].split("&")[0]

            if not item_id and "4" in message and isinstance(message["4"], dict):
                url_info = message["4"].get("reminderUrl", "")
                if isinstance(url_info, str) and "itemId=" in url_info:
                    item_id = url_info.split("itemId=")[1].split("&")[0]

            if not item_id:
                item_id = self.extract_item_id_from_message(message)
        except Exception as item_e:
            logger.warning(f"{log_prefix} 提取商品ID失败: {self._safe_str(item_e)}")

        return {
            'buyer_id': buyer_id,
            'buyer_id_source': buyer_id_source,
            'buyer_nick': buyer_nick,
            'sid': sid,
            'item_id': item_id,
        }

    def _preload_basic_order_info(self, order_id: str, item_id: str = None, buyer_id: str = None,
                                  sid: str = None, buyer_nick: str = None,
                                  buyer_id_source: str = None) -> bool:
        try:
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error(
                    f"【default】基础订单预入库缺少 canonical account_id，拒绝继续运行: {order_id}"
                )
                return False
            existing_order = db_manager.get_order_by_id(order_id, account_id=current_account_id)
            if not existing_order:
                logger.warning(
                    f"【{self.account_id}】基础订单预入库命中未验证归属，拒绝首写 scoped order: "
                    f"order_id={order_id}, account_id={current_account_id}"
                )
                return False
            buyer_id_to_save, buyer_nick_to_save, should_skip_write = self._select_buyer_identity_for_order_write(
                order_id,
                incoming_buyer_id=buyer_id,
                incoming_buyer_nick=buyer_nick,
                existing_order=existing_order,
                buyer_id_source=buyer_id_source,
                buyer_nick_source="preload",
                log_prefix=f"【{self.account_id}?",
            )
            if should_skip_write:
                return False

            success = db_manager.insert_or_update_order(
                order_id=order_id,
                item_id=item_id,
                buyer_id=buyer_id_to_save,
                buyer_nick=buyer_nick_to_save,
                sid=sid,
                account_id=current_account_id,
                order_status='processing' if not existing_order else None
            )
            if success:
                action = "更新基础订单信息" if existing_order else "基础订单已预入库"
                logger.info(
                    f"【{self.account_id}】{action}: order_id={order_id}, item_id={item_id}, "
                    f"buyer_id={buyer_id_to_save}, sid={sid or '-'}"
                )
            else:
                logger.warning(f"【{self.account_id}】基础订单预入库失败: {order_id}")
            return success
        except Exception as e:
            logger.error(f"【{self.account_id}】基础订单预入库异常: {self._safe_str(e)}")
            return False

    async def _retry_order_detail_after_delay(self, order_id: str, item_id: str = None, buyer_id: str = None,
                                              sid: str = None, buyer_nick: str = None, delay_seconds: int = 30,
                                              buyer_id_source: str = None, account_id: str = None):
        current_task = asyncio.current_task()
        scheduled_account_id = self._normalize_account_scope(account_id)
        if not scheduled_account_id:
            scheduled_account_id = self._canonical_account_id()
        if not scheduled_account_id:
            logger.error(
                f"【default】订单详情延迟补抓缺少 canonical account_id，拒绝继续运行 {order_id}"
            )
            return
        scope_key = self._compose_order_detail_scope_key(scheduled_account_id, order_id)
        if not scope_key:
            return
        try:
            await asyncio.sleep(delay_seconds)
            current_account_id = self._canonical_account_id()
            if current_account_id and current_account_id != scheduled_account_id:
                logger.warning(
                    f"【{scheduled_account_id}】订单详情延迟补抓检测到实例账号作用域已变化，拒绝跨账号继续运行: "
                    f"order_id={order_id}, current_account_id={current_account_id}"
                )
                return

            logger.info(f"【{scheduled_account_id}】开始延迟补抓订单详情: order_id={order_id}, delay={delay_seconds}s")
            result = await self.fetch_order_detail_info(
                order_id,
                item_id,
                buyer_id,
                sid=sid,
                buyer_nick=buyer_nick,
                buyer_id_source=buyer_id_source,
                force_refresh=True
            )
            if result:
                logger.info(f"【{scheduled_account_id}】订单详情延迟补抓成功: {order_id}")
            else:
                logger.warning(f"【{scheduled_account_id}】订单详情延迟补抓仍失败，保留基础订单: {order_id}")
        except asyncio.CancelledError:
            logger.info(f"【{scheduled_account_id}】订单详情延迟补抓任务已取消: {order_id}")
            raise
        except Exception as e:
            logger.error(f"【{scheduled_account_id}】订单详情延迟补抓异常: {order_id} - {self._safe_str(e)}")
        finally:
            existing_task = self.order_detail_retry_tasks.get(scope_key)
            if existing_task is current_task:
                self.order_detail_retry_tasks.pop(scope_key, None)

    def _schedule_order_detail_retry(self, order_id: str, item_id: str = None, buyer_id: str = None,
                                     sid: str = None, buyer_nick: str = None, delay_seconds: int = 30,
                                     buyer_id_source: str = None):
        canonical_account_id = self._canonical_account_id()
        if not canonical_account_id:
            logger.error(
                f"【default】订单详情补抓调度缺少 canonical account_id，拒绝继续运行 {order_id}"
            )
            return

        scope_key = self._compose_order_detail_scope_key(canonical_account_id, order_id)
        if not scope_key:
            return

        existing_task = self.order_detail_retry_tasks.get(scope_key)
        if existing_task and not existing_task.done():
            logger.info(f"【{canonical_account_id}】订单详情补抓任务已存在，跳过重复调度: {order_id}")
            return

        task = self._create_tracked_task(
            self._retry_order_detail_after_delay(
                order_id,
                item_id=item_id,
                buyer_id=buyer_id,
                sid=sid,
                buyer_nick=buyer_nick,
                delay_seconds=delay_seconds,
                buyer_id_source=buyer_id_source,
                account_id=canonical_account_id,
            )
        )
        self.order_detail_retry_tasks[scope_key] = task
        logger.info(f"【{canonical_account_id}】已调度订单详情补抓任务: order_id={order_id}, delay={delay_seconds}s")


    def _get_message_priority(self, message_data: dict) -> int:
        try:
            if isinstance(message_data, dict):
                if message_data.get("code") == 200 and "body" not in message_data:
                    return 0

                body = message_data.get("body", {})

                if "syncPushPackage" in body:
                    try:
                        sync_data = body["syncPushPackage"].get("data", [])
                        if sync_data and isinstance(sync_data, list) and len(sync_data) > 0:
                            first_data = sync_data[0]
                            data_str = str(first_data).lower()
                            if any(kw in data_str for kw in ['orderid', 'order_id', 'bizorderid', 'paysucc', 'paid']):
                                return 1
                            if 'message' in data_str or 'chat' in data_str:
                                return 2
                    except Exception:
                        pass

                if message_data.get("code") == 200:
                    return 0

            return 3
        except Exception as e:
            logger.debug(f"【{self.account_id}】解析消息优先级失败: {e}")
            return 3

    async def _enqueue_message(self, message_data: dict, websocket, msg_id: str = "unknown") -> bool:
        try:
            priority = self._get_message_priority(message_data)

            async with self.message_queue_lock:
                self.message_queue_counter += 1
                counter = self.message_queue_counter

            message_item = {
                'data': message_data,
                'websocket': websocket,
                'msg_id': msg_id,
                'enqueue_time': time.time(),
                'priority': priority,
            }

            try:
                self.message_queue.put_nowait((priority, counter, message_item))
                self.queue_stats['received'] += 1

                if priority <= 1:
                    logger.info(f"【{self.account_id}】📥 高优先级消息入队 [P{priority}][ID:{msg_id}] 队列大小: {self.message_queue.qsize()}")
                else:
                    logger.debug(f"【{self.account_id}】📥 消息入队 [P{priority}][ID:{msg_id}] 队列大小: {self.message_queue.qsize()}")

                return True
            except asyncio.QueueFull:
                self.queue_stats['dropped_full'] += 1
                logger.warning(f"【{self.account_id}】⚠️ 消息队列已满({self.message_queue_max_size})，消息[ID:{msg_id}]被丢弃")
                return False

        except Exception as e:
            logger.error(f"【{self.account_id}】消息入队失败: {self._safe_str(e)}")
            return False

    async def _message_worker(self, worker_id: int):
        logger.info(f"【{self.account_id}】🔧 消息处理工作协程 #{worker_id} 启动")

        while self.message_queue_running:
            try:
                try:
                    priority, counter, message_item = await asyncio.wait_for(
                        self.message_queue.get(),
                        timeout=5.0
                    )
                except asyncio.TimeoutError:
                    continue

                enqueue_time = message_item['enqueue_time']
                age = time.time() - enqueue_time
                if age > self.message_expire_seconds:
                    self.queue_stats['dropped_expired'] += 1
                    logger.warning(f"【{self.account_id}】⏰ 工作协程#{worker_id} 丢弃过期消息 [ID:{message_item['msg_id']}] 已等待{age:.1f}秒")
                    self.message_queue.task_done()
                    continue

                msg_id = message_item['msg_id']
                try:
                    logger.debug(f"【{self.account_id}】🔄 工作协程#{worker_id} 开始处理消息 [P{priority}][ID:{msg_id}] 等待{age:.2f}秒")

                    async with self.message_semaphore:
                        self.active_message_tasks += 1
                        try:
                            await self.handle_message(
                                message_item['data'],
                                message_item['websocket'],
                                msg_id
                            )
                            self.queue_stats['processed'] += 1
                        finally:
                            self.active_message_tasks -= 1

                    logger.debug(f"【{self.account_id}】✅ 工作协程#{worker_id} 完成消息处理 [ID:{msg_id}]")

                except Exception as e:
                    self.queue_stats['errors'] += 1
                    logger.error(f"【{self.account_id}】❌ 工作协程#{worker_id} 处理消息失败 [ID:{msg_id}]: {self._safe_str(e)}")
                finally:
                    self.message_queue.task_done()

            except asyncio.CancelledError:
                logger.info(f"【{self.account_id}】🛑 消息处理工作协程 #{worker_id} 被取消")
                break
            except Exception as e:
                logger.error(f"【{self.account_id}】工作协程#{worker_id} 异常: {self._safe_str(e)}")
                await asyncio.sleep(1)
        logger.info(f"【{self.account_id}】🔧 消息处理工作协程 #{worker_id} 已停止")

    async def _start_message_queue_workers(self):
        if not self.message_queue_enabled:
            logger.info(f"【{self.account_id}】消息队列系统已禁用，使用传统处理模式")
            return

        self.message_queue_running = True
        self.message_workers = []

        for i in range(self.message_queue_workers):
            worker_task = self._create_tracked_task(self._message_worker(i))
            self.message_workers.append(worker_task)

        self._create_tracked_task(self._queue_stats_monitor())

        logger.info(f"【{self.account_id}】🚀 消息队列系统已启动，{self.message_queue_workers}个工作协程")

    async def _stop_message_queue_workers(self):
        self.message_queue_running = False

        for worker_task in self.message_workers:
            if not worker_task.done():
                worker_task.cancel()

        if self.message_workers:
            await asyncio.gather(*self.message_workers, return_exceptions=True)

        self.message_workers = []
        logger.info(f"【{self.account_id}】🛑 消息队列系统已停止")

    async def _queue_stats_monitor(self):
        while self.message_queue_running:
            try:
                await asyncio.sleep(60)
                if not self.message_queue_running:
                    break

                stats = self.queue_stats
                elapsed = time.time() - stats['last_stats_time']

                if stats['received'] > 0:
                    process_rate = stats['processed'] / elapsed if elapsed > 0 else 0
                    drop_rate = (stats['dropped_full'] + stats['dropped_expired']) / stats['received'] * 100

                    logger.info(
                        f"【{self.account_id}】📊 消息队列统计 - "
                        f"队列大小: {self.message_queue.qsize()}/{self.message_queue_max_size} | "
                        f"收到: {stats['received']} | "
                        f"处理: {stats['processed']} | "
                        f"丢弃(满): {stats['dropped_full']} | "
                        f"丢弃(过期): {stats['dropped_expired']} | "
                        f"错误: {stats['errors']} | "
                        f"处理速率: {process_rate:.1f}/s | "
                        f"丢弃率: {drop_rate:.1f}%"
                    )

                    if drop_rate > 10:
                        logger.warning(f"【{self.account_id}】⚠️ 消息丢弃率过高({drop_rate:.1f}%)，建议增加工作协程数量或检查消息处理效率")

                stats['last_stats_time'] = time.time()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"【{self.account_id}】队列监控异常: {self._safe_str(e)}")

    def is_auto_confirm_enabled(self) -> bool:
        try:
            from db_manager import db_manager
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error("【default】自动确认发货开关检查缺少 canonical account_id，拒绝继续运行")
                return False
            return db_manager.get_auto_confirm(current_account_id)
        except Exception as e:
            logger.error(f"【{self.account_id}】获取自动确认发货设置失败: {self._safe_str(e)}")
            return False

    def is_auto_comment_enabled(self) -> bool:
        try:
            from db_manager import db_manager
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error("【default】自动好评开关检查缺少 canonical account_id，拒绝继续运行")
                return False
            return db_manager.get_auto_comment(current_account_id)
        except Exception as e:
            logger.error(f"【{self.account_id}】获取自动好评设置失败: {self._safe_str(e)}")
            return False
    async def handle_auto_comment(self, message: dict, msg_time: str, msg_id: str = ""):
        try:
            if not self.is_auto_comment_enabled():
                logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 未启用自动好评，跳过')
                return False

            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error(
                    f'[{msg_time}] 【default】[{msg_id}] 自动好评缺少 canonical account_id，拒绝继续运行'
                )
                return False

            order_id = self._extract_order_id_for_comment(message)
            if not order_id:
                logger.warning(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 无法从评价消息中提取订单ID，跳过自动好评')
                return False

            logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 棢测到评价提醒，订单ID: {order_id}')

            from db_manager import db_manager
            template = db_manager.get_active_comment_template(current_account_id)
            if not template:
                logger.warning(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 未设置激活的好评模板，跳过自动好评')
                return False

            comment_content = template.get('content', '')
            if not comment_content:
                logger.warning(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 好评模板内容为空，跳过自动好评')
                return False

            logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 使用模板"{template.get("name", "")}"进行好评: {comment_content[:50]}...')

            result = await self._call_comment_api(order_id, comment_content)

            if result.get('success'):
                logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] ✅ 订单 {order_id} 自动好评成功')
                return True
            else:
                logger.warning(f'[{msg_time}] 【{self.account_id}】[{msg_id}] ❌ 订单 {order_id} 自动好评失败: {result.get("message", "未知错误")}')
                return False

        except Exception as e:
            logger.error(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 自动好评异常: {self._safe_str(e)}')
            return False

    def _extract_order_id_for_comment(self, message: dict) -> str:
        try:
            order_id = self._extract_order_id(message)
            if order_id:
                logger.info(f'【{self.account_id}】评价提醒消息提取到订单ID: {order_id}')
            return order_id

        except Exception as e:
            logger.error(f"【{self.account_id}】提取评价订单ID失败: {self._safe_str(e)}")
            return None

    async def _call_comment_api(self, order_id: str, comment: str) -> dict:
        import aiohttp

        try:
            comment_api_url = (
                (db_manager.get_system_setting('auto_comment_api_url') or '').strip()
                or str(os.getenv('AUTO_COMMENT_API_URL') or '').strip()
            )
            if not comment_api_url:
                logger.warning(f"【{self.account_id}】未配置自动好评辅助API地址，已阻止向未知第三方发Cookie")
                return {
                    "success": False,
                    "message": "未配置自动好评辅助API地址"
                }

            cookie_str = self.cookies_str

            payload = {
                "cookie_str": cookie_str,
                "order_id": order_id,
                "comment": comment
            }

            headers = {
                "accept": "application/json",
                "Content-Type": "application/json"
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(comment_api_url, json=payload, headers=headers, timeout=30) as response:
                    if response.status == 200:
                        result = await response.json()
                        return {
                            "success": result.get("status") == "success",
                            "message": result.get("message", "好评成功")
                        }
                    else:
                        error_text = await response.text()
                        logger.error(f"【{self.account_id}】好评接口返回错误: status={response.status}, body={error_text}")
                        return {
                            "success": False,
                            "message": f"接口返回错误: {response.status}"
                        }

        except asyncio.TimeoutError:
            logger.error(f"【{self.account_id}】好评接口请求超时")
            return {
                "success": False,
                "message": "请求超时"
            }
        except Exception as e:
            logger.error(f"【{self.account_id}】调用好评接口异常: {self._safe_str(e)}")
            return {
                "success": False,
                "message": str(e)
            }

    def can_auto_delivery(self, order_id: str) -> bool:
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id:
            logger.error("【default】订单自动发货冷却检查缺少有效 order_id，拒绝继续运行")
            return False

        current_account_id = self._canonical_account_id()
        if not current_account_id:
            logger.error(
                f"【default】订单自动发货冷却检查缺少 canonical account_id，拒绝继续运行 {normalized_order_id}"
            )
            return False

        delivery_scope_key = self._compose_order_delivery_scope_key(current_account_id, normalized_order_id)
        if not delivery_scope_key:
            logger.error(
                f"【{current_account_id}】订单自动发货冷却检查缺少有效作用域键，拒绝继续运行: {normalized_order_id}"
            )
            return False

        current_time = time.time()
        last_delivery = self.last_delivery_time.get(delivery_scope_key, 0)

        if current_time - last_delivery < self.delivery_cooldown:
            logger.info(f"【{current_account_id}】订单 {normalized_order_id} 在冷却期内，跳过自动发货")
            return False

        return True

    def mark_delivery_sent(self, order_id: str, context: str = "自动发货完成"):
        current_account_id = self._canonical_account_id()
        normalized_order_id = str(order_id or "").strip()
        if not current_account_id:
            logger.error(
                f"【default】订单发货标记缺少 canonical account_id，拒绝继续运行 {normalized_order_id}"
            )
            return False
        delivery_scope_key = self._compose_order_delivery_scope_key(current_account_id, normalized_order_id)
        if not delivery_scope_key:
            logger.error(
                f"【{current_account_id}】订单发货标记缺少有效作用域键，拒绝继续运行: {normalized_order_id}"
            )
            return False
        self.delivery_sent_orders.add(delivery_scope_key)
        self.last_delivery_time[delivery_scope_key] = time.time()
        logger.info(f"【{current_account_id}】订单 {normalized_order_id} 已标记为发货")

        logger.info(f"【{current_account_id}】检查自动发货订单状态处理器: handler_exists={self.order_status_handler is not None}")
        if self.order_status_handler:
            logger.info(f"【{current_account_id}】准备调用订单状态处理器.handle_auto_delivery_order_status: {normalized_order_id}")
            try:
                success = self.order_status_handler.handle_auto_delivery_order_status(
                    order_id=normalized_order_id,
                    account_id=current_account_id,
                    context=context
                )
                logger.info(f"【{current_account_id}】订单状态处理器.handle_auto_delivery_order_status返回结果: {success}")
                if success:
                    logger.info(f"【{current_account_id}】订单 {normalized_order_id} 状态已更新为已发货")
                else:
                    logger.warning(f"【{current_account_id}】订单 {normalized_order_id} 状态更新为已发货失败")
            except Exception as e:
                logger.error(f"【{current_account_id}】订单状态更新失败: {self._safe_str(e)}")
                import traceback
                logger.error(f"【{current_account_id}】详细错误信息: {traceback.format_exc()}")
        else:
            logger.warning(f"【{current_account_id}】订单状态处理器为None，跳过自动发货状态更新: {normalized_order_id}")
        return True

    def _activate_delivery_lock(self, lock_key: str, delay_minutes: int = 10):
        if not lock_key:
            return

        existing_lock = self._lock_hold_info.get(lock_key)
        if existing_lock and existing_lock.get('locked'):
            return

        self._lock_hold_info[lock_key] = {
            'locked': True,
            'lock_time': time.time(),
            'release_time': None,
            'task': None
        }
        delay_task = asyncio.create_task(self._delayed_lock_release(lock_key, delay_minutes=delay_minutes))
        self._lock_hold_info[lock_key]['task'] = delay_task

    def _record_delivery_log(self, order_id: str = None, item_id: str = None, buyer_id: str = None,
                             buyer_nick: str = None, status: str = 'failed', reason: str = None,
                             channel: str = 'auto', rule_meta: dict = None):
        try:
            from db_manager import db_manager
            meta = rule_meta or {}
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error(
                    "【default】发货日志缺少 canonical account_id，拒绝继续运行"
                )
                return False
            log_prefix = f"【{self.account_id}?"
            resolved_buyer_nick = self._resolve_delivery_log_buyer_nick(
                buyer_nick,
                order_id=order_id,
                buyer_id=buyer_id,
                log_prefix=log_prefix,
            )
            normalized_status = str(status or 'failed').strip().lower()
            if normalized_status not in {'success', 'failed', 'skipped'}:
                normalized_status = 'failed'
            db_manager.create_delivery_log(
                user_id=self.user_id,
                account_id=current_account_id,
                order_id=order_id,
                item_id=item_id,
                buyer_id=buyer_id,
                buyer_nick=resolved_buyer_nick,
                rule_id=meta.get('rule_id'),
                rule_keyword=meta.get('rule_keyword'),
                card_type=meta.get('card_type'),
                match_mode=meta.get('match_mode'),
                channel=channel or 'auto',
                status=normalized_status,
                reason=self._format_delivery_log_reason(reason, meta)
            )
            return True
        except Exception as log_e:
            logger.error(f"【{self.account_id}】记录发货日志失败: {self._safe_str(log_e)}")
            return False

    def _format_delivery_log_reason(self, reason: str = None, rule_meta: dict = None) -> str:
        meta = rule_meta or {}
        context_parts = []

        order_spec_mode = meta.get('order_spec_mode')
        rule_spec_mode = meta.get('rule_spec_mode')
        item_config_mode = meta.get('item_config_mode')

        if order_spec_mode:
            context_parts.append(f"order_spec_mode={order_spec_mode}")
        if rule_spec_mode:
            context_parts.append(f"rule_spec_mode={rule_spec_mode}")
        if item_config_mode:
            context_parts.append(f"item_config_mode={item_config_mode}")

        reason_text = (reason or '').strip()
        if not context_parts:
            return reason_text

        if any(part.split('=')[0] + '=' in reason_text for part in context_parts):
            return reason_text

        if not reason_text:
            reason_text = '未提供发货日志原因'

        return f"{reason_text} [{', '.join(context_parts)}]"

    async def _finalize_delivery_after_send(self, delivery_meta: dict = None, order_id: str = None,
                                            item_id: str = None, skip_confirm: bool = False):
        meta = delivery_meta or {}
        current_account_id = self._canonical_account_id()

        if not meta.get('success'):
            return {
                'success': False,
                'error': '发货元数据无效，无法提交副作用'
            }

        if not current_account_id:
            logger.error(
                "【default】发货收尾缺少 canonical account_id，拒绝继续运行"
            )
            return {
                'success': False,
                'error': 'missing canonical account_id for delivery finalization'
            }

        from db_manager import db_manager
        normalized_order_id = str(order_id or "").strip()

        if not normalized_order_id:
            logger.error(
                f"【{current_account_id}】发货收尾缺少有效 order_id，拒绝继续提交副作用"
            )
            return {
                'success': False,
                'error': 'missing order_id for delivery finalization'
            }

        scoped_order = db_manager.get_order_by_id(
            normalized_order_id,
            account_id=current_account_id,
        )
        if not scoped_order:
            logger.error(
                f"【{current_account_id}】发货收尾订单归属校验失败，拒绝继续提交副作用 "
                f"order_id={normalized_order_id}"
            )
            return {
                'success': False,
                'error': f'order {normalized_order_id} is outside current account scope'
            }

        consume_required = bool(meta.get('data_card_pending_consume'))
        rule_id = meta.get('rule_id')
        card_id = meta.get('card_id')
        card_type = meta.get('card_type')
        expected_line = meta.get('data_line')
        reservation_id = meta.get('data_reservation_id')
        reservation_already_finalized = False

        if consume_required:
            if reservation_id:
                finalize_state = db_manager.finalize_batch_data_reservation(reservation_id)
                if not finalize_state.get('success'):
                    return {
                        'success': False,
                        'error': '批量数据预占完成失败，已中止后续确认发货'
                    }
                reservation_already_finalized = bool(finalize_state.get('already_finalized'))
            elif not card_id or card_type != 'data':
                return {
                    'success': False,
                    'error': '批量数据卡券元数据不完整，无法提交消费'
                }
            else:
                consumed = db_manager.consume_specific_batch_data(card_id, expected_line)
                if not consumed:
                    return {
                        'success': False,
                        'error': '批量数据消费失败，已中止后续确认发货'
                    }

        if rule_id and not consume_required:
            db_manager.increment_delivery_times(rule_id)

        if normalized_order_id and not skip_confirm:
            if not self.is_auto_confirm_enabled():
                logger.info(f"自动确认发货已关闭，跳过订单 {normalized_order_id}")
            else:
                current_time = time.time()
                should_confirm = True
                confirm_scope_key = self._compose_order_delivery_scope_key(
                    current_account_id,
                    normalized_order_id,
                )

                if confirm_scope_key and confirm_scope_key in self.confirmed_orders:
                    last_confirm_time = self.confirmed_orders[confirm_scope_key]
                    if current_time - last_confirm_time < self.order_confirm_cooldown:
                        logger.info(
                            f"订单 {normalized_order_id} 已在 {self.order_confirm_cooldown} 秒内确认过，跳过重复确认"
                        )
                        should_confirm = False

                if should_confirm:
                    logger.info(f"开始自动确认发货: 订单ID={normalized_order_id}, 商品ID={item_id}")
                    confirm_result = await self.auto_confirm(normalized_order_id, item_id)
                    if confirm_result.get('success'):
                        if confirm_scope_key:
                            self.confirmed_orders[confirm_scope_key] = current_time
                        logger.info(f"🎉 自动确认发货成功！订单ID: {normalized_order_id}")
                    else:
                        return {
                            'success': False,
                            'error': f"自动确认发货失败: {confirm_result.get('error', '未知错误')}"
                        }

        if rule_id and consume_required and not reservation_already_finalized:
            db_manager.increment_delivery_times(rule_id)

        return {
            'success': True
        }

    def _mark_data_reservation_sent_if_needed(self, delivery_meta: dict = None) -> bool:
        meta = delivery_meta or {}
        reservation_id = meta.get('data_reservation_id')
        if not reservation_id:
            return True

        from db_manager import db_manager
        return db_manager.mark_batch_data_reservation_sent(reservation_id)

    def _release_data_reservation_if_needed(self, delivery_meta: dict = None, error: str = None) -> bool:
        meta = delivery_meta or {}
        reservation_id = meta.get('data_reservation_id')
        if not reservation_id:
            return True

        from db_manager import db_manager
        return db_manager.release_batch_data_reservation(reservation_id, error=error)

    def _get_pending_delivery_finalization_meta(self, order_id: str, delivery_unit_index: int = 1):
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id:
            return None

        from db_manager import db_manager
        current_account_id = self._canonical_account_id()
        if not current_account_id:
            logger.error(
                f"【default】读取发货收尾状态缺少 canonical account_id，拒绝继续运行 {order_id}"
            )
            return None
        state = db_manager.get_delivery_finalization_state(
            normalized_order_id,
            delivery_unit_index,
            account_id=current_account_id,
        )
        if not state or state.get('status') != 'sent':
            return None

        delivery_meta = state.get('delivery_meta') or {}
        delivery_meta.setdefault('success', True)
        delivery_meta.setdefault('delivery_unit_index', delivery_unit_index)
        return delivery_meta

    def _persist_delivery_finalization_state(self, order_id: str, item_id: str, buyer_id: str,
                                             delivery_meta: dict = None, channel: str = 'auto',
                                             status: str = 'sent', last_error: str = None) -> bool:
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id:
            return False

        from db_manager import db_manager
        current_account_id = self._canonical_account_id()
        if not current_account_id:
            logger.error(
                f"【default】发货收尾状态缺少 canonical account_id，拒绝继续运行 {order_id}"
            )
            return False
        normalized_status = str(status or "sent").strip().lower() or "sent"
        scoped_order = db_manager.get_order_by_id(
            normalized_order_id,
            account_id=current_account_id,
        )
        if not scoped_order:
            if normalized_status == 'finalized':
                logger.error(
                    f"【{current_account_id}】发货收尾状态拒绝写入 finalized：订单不在当前账号作用域内: "
                    f"order_id={normalized_order_id}"
                )
                return False
            logger.warning(
                f"【{current_account_id}】发货收尾状态未命中 scoped order，保留 sent 补偿标记: "
                f"order_id={normalized_order_id}"
            )
        meta = delivery_meta or {}
        unit_index = int(meta.get('delivery_unit_index') or 1)
        return db_manager.upsert_delivery_finalization_state(
            order_id=normalized_order_id,
            unit_index=unit_index,
            account_id=current_account_id,
            item_id=item_id,
            buyer_id=buyer_id,
            channel=channel,
            status=normalized_status,
            delivery_meta=meta,
            last_error=last_error,
        )

    def _summarize_delivery_progress(self, order_id: str, expected_quantity: int = 1):
        default_summary = {
            'order_id': order_id,
            'expected_quantity': max(1, int(expected_quantity or 1)),
            'aggregate_status': 'pending_ship',
            'finalized_count': 0,
            'pending_finalize_count': 0,
            'remaining_count': max(1, int(expected_quantity or 1)),
            'finalized_unit_indexes': [],
            'pending_finalize_unit_indexes': [],
            'remaining_unit_indexes': list(range(1, max(1, int(expected_quantity or 1)) + 1)),
            'states': [],
        }
        if not order_id:
            return default_summary

        from db_manager import db_manager
        current_account_id = self._canonical_account_id()
        if not current_account_id:
            logger.error(
                f"【default】读取发货进度缺少 canonical account_id，拒绝继续运行 {order_id}"
            )
            return default_summary
        return db_manager.get_delivery_progress_summary(
            order_id,
            account_id=current_account_id,
            expected_quantity=expected_quantity,
        )

    def _resolve_external_order_status(self, current_status: str, incoming_status: str, source: str):
        from db_manager import db_manager

        merged_status = db_manager.resolve_external_order_status(current_status, incoming_status, source=source)
        normalized_current = db_manager._normalize_order_status(current_status)

        if merged_status and merged_status != normalized_current:
            return merged_status
        return None

    def _normalize_order_amount_text(self, value: Any):
        text = str(value or '').strip()
        if not text:
            return None
        text = text.replace('¥', '').replace('\uffe5', '').replace(',', '')
        match = re.search(r'\d+(?:\.\d{1,2})?', text)
        if not match:
            return None
        try:
            return f"{float(match.group(0)):.2f}"
        except (TypeError, ValueError):
            return None

    def _parse_order_amount_float(self, value: Any):
        normalized = self._normalize_order_amount_text(value)
        if normalized is None:
            return None
        try:
            return float(normalized)
        except (TypeError, ValueError):
            return None

    def _has_bargain_success_evidence(self, order: dict = None) -> bool:
        order = order or {}
        return bool(order.get('bargain_success_detected'))

    def _mark_order_bargain_flow(self, order_id: str, item_id: str = None, buyer_id: str = None,
                                 sid: str = None, *, apply_configured_price: bool = False,
                                 success_detected=..., context: str = '') -> bool:
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id:
            return False

        from db_manager import db_manager

        current_account_id = self._canonical_account_id()
        if not current_account_id:
            logger.error(
                f"【default】小刀订单标记缺少 canonical account_id，拒绝继续运行 {normalized_order_id or order_id}"
            )
            return False
        existing_order = db_manager.get_order_by_id(normalized_order_id, account_id=current_account_id) or {}
        if not existing_order:
            logger.warning(
                f"【{self.account_id}】小刀订单标记命中未验证归属，拒绝首写 scoped order: "
                f"order_id={normalized_order_id}, account_id={current_account_id}"
            )
            return False
        effective_item_id = item_id or existing_order.get('item_id')
        effective_buyer_id = buyer_id or existing_order.get('buyer_id')
        effective_sid = sid or existing_order.get('sid')
        amount_to_save = None

        if apply_configured_price and effective_item_id:
            item_config = db_manager.get_item_info(current_account_id, effective_item_id)
            configured_amount = self._normalize_order_amount_text(item_config.get('item_price') if item_config else None)
            configured_amount_value = self._parse_order_amount_float(configured_amount)
            existing_amount_value = self._parse_order_amount_float(existing_order.get('amount'))
            if configured_amount_value is not None and (
                existing_amount_value is None or configured_amount_value < existing_amount_value - 0.009
            ):
                amount_to_save = configured_amount

        success = db_manager.insert_or_update_order(
            order_id=normalized_order_id,
            item_id=effective_item_id,
            buyer_id=effective_buyer_id,
            sid=effective_sid,
            amount=amount_to_save,
            account_id=current_account_id,
            bargain_flow_detected=True,
            bargain_success_detected=success_detected,
        )

        if success:
            logger.info(
                f"【{self.account_id}】标记订单为小刀流程: order_id={normalized_order_id}, context={context or 'unknown'}, "
                f"apply_configured_price={apply_configured_price}, amount_override={amount_to_save or ''}, "
                f"success_detected={success_detected if success_detected is not ... else 'unchanged'}"
            )
        else:
            logger.warning(
                f"【{self.account_id}】标记订单小刀流程失败: order_id={normalized_order_id}, context={context or 'unknown'}"
            )
        return success

    def _apply_bargain_amount_override(self, order_id: str, item_id: str, amount: Any, amount_source: str,
                                       existing_order: dict = None, item_config: dict = None):
        existing_order = existing_order or {}
        if not existing_order.get('bargain_flow_detected'):
            return amount, amount_source

        configured_amount = self._normalize_order_amount_text(item_config.get('item_price') if item_config else None)
        configured_amount_value = self._parse_order_amount_float(configured_amount)
        if configured_amount_value is None:
            return amount, amount_source

        incoming_amount = self._normalize_order_amount_text(amount)
        incoming_amount_value = self._parse_order_amount_float(incoming_amount)

        if incoming_amount_value is None:
            logger.warning(
                f"【{self.account_id}】小刀订单缺少可信金额，回退为商品配置价: "
                f"order_id={order_id}, item_id={item_id}, configured_amount={configured_amount}"
            )
            return configured_amount, 'bargain_item_price_locked'

        if incoming_amount_value > configured_amount_value + 0.009:
            logger.warning(
                f"【{self.account_id}】检测到小刀订单仍返回原价，使用商品配置价覆盖: "
                f"order_id={order_id}, item_id={item_id}, incoming_amount={incoming_amount}, "
                f"configured_amount={configured_amount}, amount_source={amount_source}"
            )
            return configured_amount, 'bargain_item_price_locked'

        return incoming_amount, amount_source

    def _resolve_delivery_progress_order_status(self, current_status: str, aggregate_status: str):
        from db_manager import db_manager

        normalized_current = db_manager._normalize_order_status(current_status)
        normalized_aggregate = db_manager._normalize_order_status(aggregate_status)

        if not normalized_aggregate or normalized_aggregate == 'unknown':
            return None

        if not normalized_current or normalized_current == 'unknown':
            return normalized_aggregate

        if normalized_current in {'completed', 'refunding', 'cancelled'} and normalized_aggregate in {
            'pending_ship', 'partial_success', 'partial_pending_finalize', 'shipped'
        }:
            logger.warning(
                f"【{self.account_id}】保留订单终态，忽略发货进度覆盖: current={normalized_current}, incoming={normalized_aggregate}"
            )
            return normalized_current

        if normalized_current == 'shipped' and normalized_aggregate in {'pending_ship', 'partial_success', 'partial_pending_finalize'}:
            logger.warning(
                f"【{self.account_id}】保留已发货状，忽略较低发货进度覆盖: current={normalized_current}, incoming={normalized_aggregate}"
            )
            return normalized_current

        if normalized_current in {'partial_success', 'partial_pending_finalize'} and normalized_aggregate == 'pending_ship':
            logger.warning(
                f"【{self.account_id}】保留部分发货状态，忽略待发货覆盖: current={normalized_current}, incoming={normalized_aggregate}"
            )
            return normalized_current

        return normalized_aggregate

    def _sync_order_delivery_progress(self, order_id: str, account_id: str, expected_quantity: int = 1,
                                      context: str = "自动发货进度同步"):
        normalized_order_id = str(order_id or "").strip()
        normalized_account_id = str(account_id or "").strip()
        canonical_account_id = self._canonical_account_id()
        if not normalized_order_id:
            logger.error(
                f"【{canonical_account_id or 'default'}】发货进度同步缺少有效 order_id，拒绝继续运行"
            )
            return self._summarize_delivery_progress(normalized_order_id, expected_quantity=expected_quantity)
        if not normalized_account_id or not canonical_account_id or normalized_account_id != canonical_account_id:
            logger.error(
                f"【default】发货进度同步缺少 canonical account_id，拒绝继续运行 {normalized_order_id}"
            )
            return self._summarize_delivery_progress(normalized_order_id, expected_quantity=expected_quantity)
        summary = self._summarize_delivery_progress(normalized_order_id, expected_quantity=expected_quantity)
        aggregate_status = summary.get('aggregate_status') or 'pending_ship'
        previous_status = None

        try:
            from db_manager import db_manager
            current_order = db_manager.get_order_by_id(
                normalized_order_id,
                account_id=canonical_account_id,
            ) if normalized_order_id else None
            previous_status = db_manager._normalize_order_status(current_order.get('order_status')) if current_order else None
        except Exception as e:
            logger.warning(f"【{self.account_id}】读取订单旧状态失败 {self._safe_str(e)}")

        logger.info(
            f"【{self.account_id}】同步订单发货进度 order_id={normalized_order_id}, status={aggregate_status}, "
            f"finalized={summary.get('finalized_count')}/{summary.get('expected_quantity')}, "
            f"pending_finalize={summary.get('pending_finalize_count')}, remaining={summary.get('remaining_count')}"
        )

        status_to_write = self._resolve_delivery_progress_order_status(previous_status, aggregate_status)

        if aggregate_status in {'shipped', 'partial_success', 'partial_pending_finalize'}:
            delivery_scope_key = self._compose_order_delivery_scope_key(canonical_account_id, normalized_order_id)
            if delivery_scope_key:
                self.delivery_sent_orders.add(delivery_scope_key)
                self.last_delivery_time[delivery_scope_key] = time.time()

        if self.order_status_handler and status_to_write == 'shipped' and previous_status != 'shipped':
            try:
                self.order_status_handler.handle_auto_delivery_order_status(
                    order_id=normalized_order_id,
                    account_id=canonical_account_id,
                    context=context
                )
            except Exception as e:
                logger.warning(f"【{self.account_id}】订单状态处理器同步已发货状态失败 {self._safe_str(e)}")

        try:
            from db_manager import db_manager
            success = True
            if status_to_write and status_to_write != previous_status:
                success = db_manager.insert_or_update_order(
                    order_id=normalized_order_id,
                    order_status=status_to_write,
                    account_id=canonical_account_id,
                )

            if success and status_to_write in {'partial_success', 'partial_pending_finalize'} and previous_status != status_to_write:
                try:
                    from order_event_hub import publish_order_update_event
                    publish_order_update_event(
                        normalized_order_id,
                        account_id=canonical_account_id,
                        source='delivery_progress_sync',
                    )
                except Exception as publish_e:
                    logger.warning(
                        f"【{self.account_id}】发布部分发货实时事件失败 order_id={normalized_order_id}, error={self._safe_str(publish_e)}"
                    )
        except Exception as e:
            logger.warning(f"【{self.account_id}】写入订单聚合发货状态失败 {self._safe_str(e)}")

        return summary

    async def _delayed_lock_release(self, lock_key: str, delay_minutes: int = 10):
        try:
            delay_seconds = delay_minutes * 60
            logger.info(f"【{self.account_id}】订单锁 {lock_key} 将在 {delay_minutes} 分钟后释放")

            await asyncio.sleep(delay_seconds)

            if lock_key in self._lock_hold_info:
                lock_info = self._lock_hold_info[lock_key]
                if lock_info.get('locked', False):
                    lock_info['release_time'] = time.time()
                    logger.info(f"【{self.account_id}】订单锁 {lock_key} 延迟释放完成")


        except asyncio.CancelledError:
            logger.info(f"【{self.account_id}】订单锁 {lock_key} 延迟释放任务被取消")
            raise
        except Exception as e:
            logger.error(f"【{self.account_id}】订单锁 {lock_key} 延迟释放失败: {self._safe_str(e)}")

    def is_lock_held(self, lock_key: str) -> bool:
        if lock_key not in self._lock_hold_info:
            return False

        lock_info = self._lock_hold_info[lock_key]
        return lock_info.get('locked', False)

    def cleanup_expired_locks(self, max_age_hours: int = 24):
        try:
            current_time = time.time()
            max_age_seconds = max_age_hours * 3600

            expired_delivery_locks = []
            for delivery_lock_key, last_used in self._lock_usage_times.items():
                if current_time - last_used > max_age_seconds:
                    expired_delivery_locks.append(delivery_lock_key)

            for delivery_lock_key in expired_delivery_locks:
                if delivery_lock_key in self._order_locks:
                    del self._order_locks[delivery_lock_key]
                if delivery_lock_key in self._lock_usage_times:
                    del self._lock_usage_times[delivery_lock_key]
                if delivery_lock_key in self._lock_hold_info:
                    lock_info = self._lock_hold_info[delivery_lock_key]
                    if 'task' in lock_info and lock_info['task']:
                        lock_info['task'].cancel()
                    del self._lock_hold_info[delivery_lock_key]

            expired_detail_locks = []
            for detail_lock_key, last_used in self._order_detail_lock_times.items():
                if current_time - last_used > max_age_seconds:
                    expired_detail_locks.append(detail_lock_key)

            for detail_lock_key in expired_detail_locks:
                if detail_lock_key in self._order_detail_locks:
                    del self._order_detail_locks[detail_lock_key]
                if detail_lock_key in self._order_detail_lock_times:
                    del self._order_detail_lock_times[detail_lock_key]

            expired_refresh_marks = []
            for refresh_scope_key, refresh_info in self.order_detail_force_refresh_marks.items():
                refresh_timestamp = refresh_info.get('timestamp', 0) if isinstance(refresh_info, dict) else 0
                if current_time - refresh_timestamp > max_age_seconds:
                    expired_refresh_marks.append(refresh_scope_key)

            for refresh_scope_key in expired_refresh_marks:
                self.order_detail_force_refresh_marks.pop(refresh_scope_key, None)

            total_expired = len(expired_delivery_locks) + len(expired_detail_locks) + len(expired_refresh_marks)
            if total_expired > 0:
                logger.info(
                    f"【{self.account_id}】清理了 {total_expired} 个过期锁/标记 "
                    f"(发货锁: {len(expired_delivery_locks)}, 详情锁: {len(expired_detail_locks)}, 刷新标记: {len(expired_refresh_marks)})"
                )
                logger.warning(f"【{self.account_id}】当前锁数量 - 发货锁: {len(self._order_locks)}, 详情锁: {len(self._order_detail_locks)}")

        except Exception as e:
            logger.error(f"【{self.account_id}】清理过期锁时发生错误: {self._safe_str(e)}")

    def _get_order_status_priority(self, status: str) -> int:
        normalized_status = db_manager._normalize_order_status(status)
        priority_map = {
            'unknown': 0,
            'processing': 10,
            'pending_payment': 15,
            'pending_ship': 20,
            'partial_success': 30,
            'partial_pending_finalize': 30,
            'shipped': 40,
            'completed': 50,
            'refunding': 60,
            'refund_cancelled': 65,
            'cancelled': 70,
        }
        return priority_map.get(normalized_status or 'unknown', 0)

    def _has_delivery_progress_evidence(self, order_id: str) -> bool:
        normalized_order_id = str(order_id or '').strip()
        if not normalized_order_id:
            return False

        try:
            summary = self._summarize_delivery_progress(normalized_order_id, expected_quantity=1) or {}
        except Exception as summary_error:
            logger.warning(
                f"【{self.account_id}】读取订单发货进度失败，按已有发货证据处理: "
                f"order_id={normalized_order_id}, error={self._safe_str(summary_error)}"
            )
            return True

        state_count = int(summary.get('state_count') or 0)
        finalized_count = int(summary.get('finalized_count') or 0)
        pending_finalize_count = int(summary.get('pending_finalize_count') or 0)
        return state_count > 0 or finalized_count > 0 or pending_finalize_count > 0

    def _reserve_order_detail_force_refresh(self, order_id: str, *, reason: str,
                                            log_prefix: str = "", cooldown_seconds: float = None) -> bool:
        normalized_order_id = str(order_id or '').strip()
        if not normalized_order_id:
            return False

        canonical_account_id = self._canonical_account_id()
        if not canonical_account_id:
            logger.error(
                f"{log_prefix} 订单详情强刷标记缺少 canonical account_id，拒绝继续运行 "
                f"order_id={normalized_order_id}"
            )
            return False

        scope_key = self._compose_order_detail_scope_key(canonical_account_id, normalized_order_id)
        if not scope_key:
            return False

        cooldown = float(cooldown_seconds or self.order_detail_force_refresh_cooldown or 0)
        now = time.time()
        existing = self.order_detail_force_refresh_marks.get(scope_key) or {}
        last_timestamp = existing.get('timestamp', 0)
        elapsed = now - last_timestamp

        if last_timestamp and cooldown > 0 and elapsed < cooldown:
            logger.info(
                f"{log_prefix} 订单详情强刷命中冷却，跳过重复强刷 "
                f"order_id={normalized_order_id}, reason={reason}, "
                f"last_reason={existing.get('reason', 'unknown')}, remaining={round(cooldown - elapsed, 2)}s"
            )
            return False

        self.order_detail_force_refresh_marks[scope_key] = {
            'timestamp': now,
            'reason': reason,
        }
        return True

    def _should_force_refresh_after_status_signal(self, status_signal: str, current_status: str,
                                                  order_id: str = None) -> bool:
        normalized_signal = db_manager._normalize_order_status(status_signal)
        normalized_current = db_manager._normalize_order_status(current_status)

        if not normalized_signal or normalized_signal == 'unknown':
            return False

        if normalized_signal == 'pending_ship':
            if normalized_current == 'shipped' and not self._has_delivery_progress_evidence(order_id):
                logger.warning(
                    f"【{self.account_id}】检测到可疑已发货状态，允许待发货信号继续强刷详情 "
                    f"order_id={order_id or 'unknown'}, current_status={normalized_current}, signal={normalized_signal}"
                )
                return True
            return normalized_current in {None, '', 'unknown', 'processing', 'pending_payment'}

        if normalized_signal == 'shipped':
            return normalized_current in {None, '', 'unknown', 'processing', 'pending_payment', 'pending_ship'}

        if normalized_signal in {'completed', 'cancelled', 'refunding', 'refund_cancelled'}:
            if not normalized_current or normalized_current == 'unknown':
                return True
            return self._get_order_status_priority(normalized_signal) > self._get_order_status_priority(normalized_current)

        return False

    def _should_accept_order_detail_status_correction(self, current_status: str, incoming_status: str,
                                                      incoming_source: str, *, force_refresh: bool,
                                                      order_id: str = None) -> bool:
        normalized_current = db_manager._normalize_order_status(current_status)
        normalized_incoming = db_manager._normalize_order_status(incoming_status)
        normalized_source = str(incoming_source or 'unknown').strip().lower()

        if not force_refresh:
            return False
        if normalized_current != 'shipped' or normalized_incoming != 'pending_ship':
            return False
        if normalized_source not in {'selector', 'button'}:
            return False
        if self._has_delivery_progress_evidence(order_id):
            return False
        return True

    def _should_reject_order_detail_status_update(self, current_status: str, incoming_status: str,
                                                  incoming_source: str, *, force_refresh: bool) -> bool:
        normalized_current = db_manager._normalize_order_status(current_status)
        normalized_incoming = db_manager._normalize_order_status(incoming_status)
        normalized_source = str(incoming_source or 'unknown').strip().lower()

        if normalized_incoming != 'completed' or normalized_source != 'body':
            return False

        if force_refresh and normalized_current in {'shipped', 'pending_ship', 'partial_success', 'partial_pending_finalize'}:
            return True

        return False

    async def _maybe_force_refresh_order_detail_for_signal(self, order_id: str, *, item_id: str = None,
                                                           buyer_id: str = None, sid: str = None,
                                                           buyer_nick: str = None, status_signal: str = None,
                                                           reason: str = "status_signal",
                                                           delay_seconds: float = 0,
                                                           log_prefix: str = "") -> bool:
        normalized_order_id = str(order_id or '').strip()
        if not normalized_order_id:
            return False

        canonical_account_id = self._canonical_account_id()
        if not canonical_account_id:
            logger.error(
                f"{log_prefix} 状态信号订单详情强刷缺少 canonical account_id，拒绝继续运行 "
                f"order_id={normalized_order_id}"
            )
            return False

        current_order = db_manager.get_order_by_id(normalized_order_id, account_id=canonical_account_id) or {}
        current_status = current_order.get('order_status')
        if not self._should_force_refresh_after_status_signal(status_signal, current_status, normalized_order_id):
            logger.info(
                f"{log_prefix} 当前订单状态无需为该信号强刷详情: order_id={normalized_order_id}, "
                f"signal={status_signal or 'unknown'}, current_status={current_status or 'unknown'}"
            )
            return False

        if not self._reserve_order_detail_force_refresh(
            normalized_order_id,
            reason=reason,
            log_prefix=log_prefix,
        ):
            return False

        if delay_seconds and delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        latest_order = db_manager.get_order_by_id(normalized_order_id, account_id=canonical_account_id) or {}
        latest_status = latest_order.get('order_status')
        if not self._should_force_refresh_after_status_signal(status_signal, latest_status, normalized_order_id):
            logger.info(
                f"{log_prefix} 延迟后订单状态已更新，无需再强刷详情: order_id={normalized_order_id}, "
                f"signal={status_signal or 'unknown'}, current_status={latest_status or 'unknown'}"
            )
            return False

        refresh_item_id = item_id or latest_order.get('item_id')
        refresh_buyer_id = buyer_id or latest_order.get('buyer_id')
        logger.info(
            f"{log_prefix} 状态信号触发订单详情强刷 order_id={normalized_order_id}, "
            f"signal={status_signal or 'unknown'}, current_status={latest_status or 'unknown'}, reason={reason}"
        )

        try:
            await self.fetch_order_detail_info(
                order_id=normalized_order_id,
                item_id=refresh_item_id,
                buyer_id=refresh_buyer_id,
                sid=sid,
                buyer_nick=buyer_nick,
                force_refresh=True
            )
            return True
        except Exception as refresh_error:
            logger.error(
                f"{log_prefix} 状态信号触发订单详情强刷失败 order_id={normalized_order_id}, "
                f"reason={reason}, error={self._safe_str(refresh_error)}"
            )
            return False


    def _load_json_dict(self, raw_value: Any) -> Dict[str, Any]:
        if isinstance(raw_value, dict):
            return raw_value
        if not isinstance(raw_value, str) or not raw_value.strip():
            return {}
        try:
            parsed = json.loads(raw_value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _extract_message_card_payload(self, message_1: Any) -> Dict[str, Any]:
        if not isinstance(message_1, dict):
            return {}

        try:
            message_6 = message_1.get('6', {})
            if not isinstance(message_6, dict):
                return {}
            message_6_3 = message_6.get('3', {})
            if not isinstance(message_6_3, dict):
                return {}
            payload = message_6_3.get('5', '')
            return self._load_json_dict(payload)
        except Exception:
            return {}

    def _extract_message_button_text(self, message_1: Any) -> str:
        payload = self._extract_message_card_payload(message_1)
        try:
            return str(
                payload.get('dxCard', {})
                .get('item', {})
                .get('main', {})
                .get('exContent', {})
                .get('button', {})
                .get('text', '')
            ).strip()
        except Exception:
            return ''

    def _extract_message_card_title(self, message_1: Any) -> str:
        payload = self._extract_message_card_payload(message_1)
        try:
            return str(
                payload.get('dxCard', {})
                .get('item', {})
                .get('main', {})
                .get('exContent', {})
                .get('title', '')
            ).strip()
        except Exception:
            return ''

    def _classify_message_route(self, *, message: dict, message_1: dict, message_10: dict,
                                send_message: str) -> Dict[str, Any]:
        message_direction = message_1.get('7', 0) if isinstance(message_1, dict) else 0
        content_type = 0
        try:
            message_6 = message_1.get('6', {}) if isinstance(message_1, dict) else {}
            if isinstance(message_6, dict):
                message_6_3 = message_6.get('3', {})
                if isinstance(message_6_3, dict):
                    content_type = message_6_3.get('4', 0)
        except Exception:
            content_type = 0

        biz_tag_raw = str(message_10.get('bizTag', '') or '').strip()
        biz_tag_dict = self._load_json_dict(biz_tag_raw)
        ext_json_dict = self._load_json_dict(message_10.get('extJson', ''))
        task_name = str(biz_tag_dict.get('taskName') or '').strip()
        update_key = str(ext_json_dict.get('updateKey') or '').strip()
        detail_notice = str(message_10.get('detailNotice', '') or '').strip()
        reminder_content = str(message_10.get('reminderContent', '') or send_message or '').strip()
        reminder_title = str(message_10.get('reminderTitle', '') or '').strip()
        reminder_notice = str(message_10.get('reminderNotice', '') or '').strip()
        red_reminder = ''
        if isinstance(message, dict) and isinstance(message.get('3'), dict):
            red_reminder = str(message.get('3', {}).get('redReminder', '') or '').strip()

        button_text = self._extract_message_button_text(message_1)
        card_title = self._extract_message_card_title(message_1)
        session_type = str(message_10.get('sessionType', '1') or '1').strip()
        is_group_message = session_type == '30'
        is_system_biz = bool(task_name) or 'SECURITY' in biz_tag_raw or 'taskId' in biz_tag_raw
        is_system_message = message_direction == 1 or content_type == 6 or is_system_biz

        texts = []
        for raw_text in (
            send_message,
            reminder_content,
            detail_notice,
            reminder_title,
            reminder_notice,
            red_reminder,
            task_name,
            update_key,
            button_text,
            card_title,
        ):
            normalized_text = str(raw_text or '').strip()
            if normalized_text and normalized_text not in texts:
                texts.append(normalized_text)

        special_flow_messages = {
            '[卡片消息]',
            '快给ta丢个评价吧~',
            '快给ta丢个评价吧',
        }
        special_flow_titles = {
            '我已小刀，待刀成',
            self._legacy_replace_tail('我已小刀，待刀成', '刀成', '\u5222'),
            self._legacy_drop_last_char('我已小刀,待刀成'),
            '我已成功小刀，待发货',
            self._legacy_drop_last_char('我已成功小刀,待发货'),
        }

        if send_message in special_flow_messages or card_title in special_flow_titles:
            route = 'special_flow'
            order_status_signal = None
        else:
            order_status_signal = None
            closed_markers = (
                '[你关闭了订单，钱款已原路退返]',
                '交易关闭',
                '订单关闭',
                '钱款已原路',
            )
            refund_markers = (
                '退款中',
                '退款成功',
                '退货退款',
                '退款关闭',
            )
            completed_markers = (
                '[买家确认收货，交易成功]',
                '[你已确认收货，交易成功]',
                '买家确认收货',
                '交易成功',
            )
            shipped_markers = (
                '[你已发货]',
                '已发',
                '等待买家收货',
            )
            pending_ship_markers = (
                '[我已付款，等待你发货]',
                '[已付款，待发货]',
                '我已付款，等待你发货',
                '[记得及时发货]',
                '等待你发',
                '待发',
                '去发',
                '付款完成待发',
                'TRADE_PAID_DONE_SELLER',
            )
            pending_payment_markers = (
                '[我已拍下，待付款]',
                '买家已拍下，待付',
                '待付',
                '等待买家付款',
                '已拍下_未付',
            )
            system_notice_markers = (
                '闲鱼小红',
                '温馨提醒',
                '曝光',
                '蚂蚁森林',
                '能量可领',
                '创建合约',
                '假客服骗',
                '订单即将自动确认收货',
                '宝贝性价比如何，去表个吧',
                '发来丢条消',
                '发来丢条新消息',
                '已出小红',
                '已收',
            )

            def _contains_any(markers) -> bool:
                return any(marker and marker in text for text in texts for marker in markers)

            if _contains_any(closed_markers):
                order_status_signal = 'cancelled'
            elif _contains_any(refund_markers):
                order_status_signal = 'refunding'
            elif _contains_any(completed_markers):
                order_status_signal = 'completed'
            elif _contains_any(shipped_markers):
                order_status_signal = 'shipped'
            elif _contains_any(pending_ship_markers):
                order_status_signal = 'pending_ship'
            elif _contains_any(pending_payment_markers):
                order_status_signal = 'pending_payment'

            if is_system_message and order_status_signal:
                route = 'order_status'
            elif _contains_any(system_notice_markers) and (is_system_message or message_direction != 2):
                route = 'system_notice'
            elif is_system_message:
                route = 'system_notice'
            else:
                route = 'user_chat'

        should_notify = False
        if not is_group_message:
            if route == 'user_chat':
                should_notify = True
            elif route == 'order_status' and order_status_signal in {'pending_ship', 'refunding', 'cancelled'}:
                should_notify = True

        return {
            'route': route,
            'order_status_signal': order_status_signal,
            'should_notify': should_notify,
            'allow_auto_reply': route == 'user_chat',
            'is_system_message': is_system_message,
            'is_group_message': is_group_message,
            'message_direction': message_direction,
            'content_type': content_type,
            'task_name': task_name,
            'button_text': button_text,
            'card_title': card_title,
            'texts': texts,
        }

    def _is_auto_delivery_trigger(self, message: str) -> bool:

        try:
            logger.warning(f"【{self.account_id}】🔍 完整消息结构: {message}")

            for source, candidate_text in self._collect_order_id_candidate_texts(message, root='message'):
                order_id = self._extract_order_id_from_candidate_text(candidate_text, source=source)
                if order_id:
                    logger.info(f'【{self.account_id}】🎯 最终提取到订单ID: {order_id} (source={source})')
                    return order_id

            if raw_message_data:
                logger.info(f'【{self.account_id}】🔍 尝试从原始消息数据中搜索订单ID')
                for source, candidate_text in self._collect_order_id_candidate_texts(raw_message_data, root='raw_message'):
                    order_id = self._extract_order_id_from_candidate_text(candidate_text, source=source)
                    if order_id:
                        logger.info(f'【{self.account_id}】🎯 从原始消息提取到订单ID: {order_id} (source={source})')
                        return order_id

                try:
                    sync_data_list = raw_message_data.get("body", {}).get("syncPushPackage", {}).get("data", [])
                    for idx, sync_data_item in enumerate(sync_data_list[:20]):
                        if not isinstance(sync_data_item, dict) or "data" not in sync_data_item:
                            continue

                        item_data = sync_data_item.get("data")
                        if item_data is None:
                            continue

                        try:
                            decoded_data = base64.b64decode(item_data).decode("utf-8")
                        except Exception:
                            decoded_data = item_data

                        for source, candidate_text in self._collect_order_id_candidate_texts(decoded_data, root=f'raw_sync[{idx}]'):
                            order_id = self._extract_order_id_from_candidate_text(candidate_text, source=source)
                            if order_id:
                                logger.info(f'【{self.account_id}】🎯 从syncPushPackage.data提取到订单ID: {order_id} (source={source})')
                                return order_id
                except Exception as multi_data_e:
                    logger.warning(f"遍历syncPushPackage.data时出错: {multi_data_e}")

            logger.warning(f'【{self.account_id}】❌ 未能从消息中提取到订单ID')
            return None

        except Exception as e:
            logger.error(f"【{self.account_id}】提取订单ID失败: {self._safe_str(e)}")
            return None

    async def _handle_simple_message_auto_delivery(self, websocket, order_id: str, item_id: str,
                                                    user_id: str, chat_id: str, msg_time: str, msg_id: str):
        try:
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error(
                    f"【default】简化自动发货缺少 canonical account_id，拒绝继续运行: {order_id}"
                )
                return
            logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 🚀 开始处理简化消息自动发货: order_id={order_id}, item_id={item_id}')

            from db_manager import db_manager
            try:
                scoped_order = db_manager.get_order_by_id(order_id, account_id=current_account_id)
            except Exception as order_check_e:
                logger.error(
                    f'[{msg_time}] 【{self.account_id}】[{msg_id}] 查询订单归属校验失败: {self._safe_str(order_check_e)}'
                )
                scoped_order = None

            if not scoped_order:
                logger.warning(
                    f'[{msg_time}] 【{current_account_id}】[{msg_id}] 订单未验证归属，拒绝自动发货: order_id={order_id}'
                )
                self._record_delivery_log(
                    order_id=order_id,
                    item_id=item_id,
                    buyer_id=user_id,
                    status='failed',
                    reason='订单未验证归属，拒绝自动发货',
                    channel='auto'
                )
                return

            if item_id and item_id != "未知商品":
                try:
                    if not await self._ensure_item_owned_by_current_account(
                        item_id,
                        log_prefix=f'[{msg_time}] 【{self.account_id}】[{msg_id}]'
                    ):
                        logger.warning(f'[{msg_time}] 【{self.account_id}】[{msg_id}] ❌ 商品 {item_id} 不属于当前账号，跳过自动发货')
                        self._record_delivery_log(
                            order_id=order_id,
                            item_id=item_id,
                            buyer_id=user_id,
                            status='failed',
                            reason='商品不属于当前账号，跳过自动发货',
                            channel='auto'
                        )
                        return
                    logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] ✅ 商品 {item_id} 归属验证通过')
                except Exception as e:
                    logger.error(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 检查商品归属失败: {self._safe_str(e)}，跳过自动发货')
                    self._record_delivery_log(
                        order_id=order_id,
                        item_id=item_id,
                        buyer_id=user_id,
                        status='failed',
                        reason=f'检查商品归属失败: {self._safe_str(e)}',
                        channel='auto'
                    )
                    return

            if not self.can_auto_delivery(order_id):
                logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 订单 {order_id} 在冷却期内，跳过发货')
                self._record_delivery_log(
                    order_id=order_id,
                    item_id=item_id,
                    buyer_id=user_id,
                    status='skipped',
                    reason='订单在冷却期内，跳过发货',
                    channel='auto'
                )
                return

            lock_key = self._compose_order_delivery_scope_key(current_account_id, order_id)
            if not lock_key:
                logger.error(
                    f"【{current_account_id or 'default'}】简化自动发货缺少有效订单锁作用域，拒绝继续运行: {order_id}"
                )
                return

            if self.is_lock_held(lock_key):
                logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 🔒 订单 {lock_key} 延迟锁仍在持有状态，跳过发货')
                self._record_delivery_log(
                    order_id=order_id,
                    item_id=item_id,
                    buyer_id=user_id,
                    status='skipped',
                    reason='订单延迟锁持有中，跳过发',
                    channel='auto'
                )
                return

            order_lock = self._order_locks[lock_key]
            self._lock_usage_times[lock_key] = time.time()

            async with order_lock:
                logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 获取订单锁成功: {lock_key}')

                if self.is_lock_held(lock_key) or not self.can_auto_delivery(order_id):
                    logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 获取锁后检查发现订单已处理，跳过发货')
                    self._record_delivery_log(
                        order_id=order_id,
                        item_id=item_id,
                        buyer_id=user_id,
                        status='skipped',
                        reason='获取锁后发现订单已处理，跳过发货',
                        channel='auto'
                    )
                    return

                logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 📤 开始执行自动发货内容发送（发送成功后再确认发货）')

                item_title = "待获取商品信息"

                pending_finalize_meta = self._get_pending_delivery_finalization_meta(order_id, 1)
                if pending_finalize_meta:
                    finalize_result = await self._finalize_delivery_after_send(
                        delivery_meta=pending_finalize_meta,
                        order_id=order_id,
                        item_id=item_id
                    )
                    if not finalize_result.get('success'):
                        self._persist_delivery_finalization_state(
                            order_id=order_id,
                            item_id=item_id,
                            buyer_id=user_id,
                            delivery_meta=pending_finalize_meta,
                            channel='auto',
                            status='sent',
                            last_error=finalize_result.get('error') or '补完成 finalize 失败'
                        )
                        self._record_delivery_log(
                            order_id=order_id,
                            item_id=item_id,
                            buyer_id=user_id,
                            status='failed',
                            reason=finalize_result.get('error') or '棢测到已发送记录，但补完成发货收尾失败',
                            channel='auto',
                            rule_meta=pending_finalize_meta
                        )
                        await self.send_delivery_failure_notification(
                            send_user_name="买家",
                            send_user_id=user_id,
                            item_id=item_id,
                            error_message=finalize_result.get('error') or '棢测到已发送记录，但补完成发货收尾失败',
                            chat_id=chat_id
                        )
                        return

                    self._persist_delivery_finalization_state(
                        order_id=order_id,
                        item_id=item_id,
                        buyer_id=user_id,
                        delivery_meta=pending_finalize_meta,
                        channel='auto',
                        status='finalized'
                    )
                    self._sync_order_delivery_progress(
                        order_id=order_id,
                        account_id=current_account_id,
                        expected_quantity=1,
                        context="自动发货补完成收尾成功"
                    )
                    self._activate_delivery_lock(lock_key, delay_minutes=10)
                    self._record_delivery_log(
                        order_id=order_id,
                        item_id=item_id,
                        buyer_id=user_id,
                        status='success',
                        reason='棢测到发货消息已发送，本次补完成收尾成',
                        channel='auto',
                        rule_meta=pending_finalize_meta
                    )
                    await self.send_delivery_failure_notification(
                        send_user_name="买家",
                        send_user_id=user_id,
                        item_id=item_id,
                        error_message="发货成功",
                        chat_id=chat_id
                    )
                    logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] ✅ 简化消息自动发货补完成收尾成功')
                    return

                delivery_result = await self._auto_delivery(
                    item_id, item_title, order_id, user_id, chat_id, include_meta=True
                )
                if isinstance(delivery_result, dict):
                    delivery_content = delivery_result.get('content')
                    delivery_error = delivery_result.get('error')
                    delivery_steps = delivery_result.get('delivery_steps') or []
                    delivery_rule_meta = {
                        'rule_id': delivery_result.get('rule_id'),
                        'rule_keyword': delivery_result.get('rule_keyword'),
                        'card_type': delivery_result.get('card_type'),
                        'match_mode': delivery_result.get('match_mode'),
                        'order_spec_mode': delivery_result.get('order_spec_mode'),
                        'rule_spec_mode': delivery_result.get('rule_spec_mode'),
                        'item_config_mode': delivery_result.get('item_config_mode'),
                        'card_id': delivery_result.get('card_id'),
                        'card_description': delivery_result.get('card_description'),
                        'data_card_pending_consume': delivery_result.get('data_card_pending_consume'),
                        'data_line': delivery_result.get('data_line'),
                        'data_reservation_id': delivery_result.get('data_reservation_id'),
                        'data_reservation_status': delivery_result.get('data_reservation_status'),
                        'delivery_unit_index': delivery_result.get('delivery_unit_index')
                    }
                else:
                    delivery_content = delivery_result
                    delivery_error = None
                    delivery_steps = []
                    delivery_rule_meta = {}

                if delivery_content:
                    delivery_rule_meta.setdefault('success', True)
                    if not delivery_steps:
                        delivery_steps = self._build_delivery_steps(
                            delivery_content,
                            delivery_rule_meta.get('card_description', '')
                        )

                    user_url = f'https://www.goofish.com/personal?userId={user_id}'

                    try:
                        await self._send_delivery_steps(
                            websocket,
                            chat_id,
                            user_id,
                            delivery_steps,
                            user_url=user_url,
                            log_prefix=f'[{msg_time}] 【{self.account_id}】[{msg_id}] 自动发货'
                        )

                        if not self._mark_data_reservation_sent_if_needed(delivery_result if isinstance(delivery_result, dict) else delivery_rule_meta):
                            self._release_data_reservation_if_needed(
                                delivery_result if isinstance(delivery_result, dict) else delivery_rule_meta,
                                error='发送成功后标记预占已发送失败'
                            )
                            raise Exception('批量数据预占标记已发送失败')

                        self._persist_delivery_finalization_state(
                            order_id=order_id,
                            item_id=item_id,
                            buyer_id=user_id,
                            delivery_meta=delivery_result if isinstance(delivery_result, dict) else delivery_rule_meta,
                            channel='auto',
                            status='sent'
                        )

                        finalize_result = await self._finalize_delivery_after_send(
                            delivery_meta=delivery_result if isinstance(delivery_result, dict) else delivery_rule_meta,
                            order_id=order_id,
                            item_id=item_id
                        )
                        if not finalize_result.get('success'):
                            self._persist_delivery_finalization_state(
                                order_id=order_id,
                                item_id=item_id,
                                buyer_id=user_id,
                                delivery_meta=delivery_result if isinstance(delivery_result, dict) else delivery_rule_meta,
                                channel='auto',
                                status='sent',
                                last_error=finalize_result.get('error') or '发送成功但提交发货副作用失败'
                            )
                            self._record_delivery_log(
                                order_id=order_id,
                                item_id=item_id,
                                buyer_id=user_id,
                                status='failed',
                                reason=finalize_result.get('error') or '发成功但提交发货副作用失',
                                channel='auto',
                                rule_meta=delivery_rule_meta
                            )
                            await self.send_delivery_failure_notification(
                                send_user_name="买家",
                                send_user_id=user_id,
                                item_id=item_id,
                                error_message=finalize_result.get('error') or '发成功但提交发货副作用失',
                                chat_id=chat_id
                            )
                            return

                        self._persist_delivery_finalization_state(
                            order_id=order_id,
                            item_id=item_id,
                            buyer_id=user_id,
                            delivery_meta=delivery_result if isinstance(delivery_result, dict) else delivery_rule_meta,
                            channel='auto',
                            status='finalized'
                        )

                        self._sync_order_delivery_progress(
                            order_id=order_id,
                            account_id=current_account_id,
                            expected_quantity=1,
                            context="自动发货发送成功"
                        )
                        self._activate_delivery_lock(lock_key, delay_minutes=10)

                        self._record_delivery_log(
                            order_id=order_id,
                            item_id=item_id,
                            buyer_id=user_id,
                            status='success',
                            reason='自动发货步骤发成',
                            channel='auto',
                            rule_meta=delivery_rule_meta
                        )
                    except Exception as send_e:
                        self._record_delivery_log(
                            order_id=order_id,
                            item_id=item_id,
                            buyer_id=user_id,
                            status='failed',
                            reason=f'自动发货消息发送失败: {self._safe_str(send_e)}',
                            channel='auto',
                            rule_meta=delivery_rule_meta
                        )
                        raise

                    await self.send_delivery_failure_notification(
                        send_user_name="买家",
                        send_user_id=user_id,
                        item_id=item_id,
                        error_message="发货成功",
                        chat_id=chat_id
                    )

                    logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] ✅ 简化消息自动发货完成')
                else:
                    logger.warning(f'[{msg_time}] 【{self.account_id}】[{msg_id}] ❌ 未找到匹配的发货规则或获取发货内容失败')
                    self._record_delivery_log(
                        order_id=order_id,
                        item_id=item_id,
                        buyer_id=user_id,
                        status='failed',
                        reason=delivery_error or '未找到匹配的发货规则或获取发货内容失',
                        channel='auto',
                        rule_meta=delivery_rule_meta
                    )
                    await self.send_delivery_failure_notification(
                        send_user_name="买家",
                        send_user_id=user_id,
                        item_id=item_id,
                        error_message="未找到匹配的发货规则或获取发货内容失败",
                        chat_id=chat_id
                    )

        except Exception as e:
            self._release_data_reservation_if_needed(
                delivery_result if 'delivery_result' in locals() and isinstance(delivery_result, dict) else delivery_rule_meta if 'delivery_rule_meta' in locals() else None,
                error=f'自动发货发送失败: {self._safe_str(e)}'
            )
            self._record_delivery_log(
                order_id=order_id,
                item_id=item_id,
                buyer_id=user_id,
                status='failed',
                reason=f'简化消息自动发货异常: {self._safe_str(e)}',
                channel='auto'
            )
            logger.error(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 简化消息自动发货异常: {self._safe_str(e)}')
            import traceback
            logger.error(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 异常堆栈: {traceback.format_exc()}')

    async def _handle_auto_delivery(self, websocket, message: dict, send_user_name: str, send_user_id: str,
                                   item_id: str, chat_id: str, msg_time: str, message_data: dict = None):
        try:
            from db_manager import db_manager
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error(
                    "【default】自动发货缺少 canonical account_id，拒绝继续运行"
                )
                return

            order_id = self._extract_order_id(message, message_data)

            if not order_id:
                fallback_sid = None
                try:
                    message_1 = message.get('1', {}) if isinstance(message, dict) else {}
                    if isinstance(message_1, dict):
                        fallback_sid = message_1.get('2', '')

                        if not fallback_sid:
                            message_10 = message_1.get('10', {})
                            if isinstance(message_10, dict):
                                reminder_url = message_10.get('reminderUrl', '') or ''
                                sid_match = re.search(r'[?&]sid=([^&]+)', reminder_url)
                                if sid_match:
                                    fallback_sid = sid_match.group(1)
                except Exception as sid_e:
                    logger.warning(f'[{msg_time}] 【{self.account_id}】解析sid失败: {self._safe_str(sid_e)}')

                if fallback_sid:
                    try:
                        log_prefix = f'[{msg_time}] 【{self.account_id}】'
                        sid_lookup_minutes = 5
                        sid_lookup = self._lookup_delivery_order_by_sid(
                            fallback_sid,
                            item_id=item_id,
                            buyer_id=send_user_id,
                            minutes=sid_lookup_minutes,
                            log_prefix=log_prefix
                        )
                        sid_lookup = await self._refresh_sid_lookup_if_needed(
                            fallback_sid,
                            sid_lookup,
                            item_id=item_id,
                            buyer_id=send_user_id,
                            minutes=sid_lookup_minutes,
                            allow_bargain_ready=True,
                            log_prefix=log_prefix
                        )
                    except Exception as sid_query_e:
                        logger.error(f'[{msg_time}] 【{self.account_id}】sid兜底查单异常: {self._safe_str(sid_query_e)}')
                        sid_lookup = {'match_type': 'error', 'order': None}

                    recent_order = sid_lookup.get('order')
                    sid_match_type = sid_lookup.get('match_type', 'missing')

                    if recent_order and sid_match_type in {'pending_ship', 'bargain_ready'}:
                        fallback_order_id = recent_order.get('order_id')
                        fallback_item_id = recent_order.get('item_id')
                        fallback_buyer_id = recent_order.get('buyer_id')

                        if send_user_id and fallback_buyer_id and self._is_trustworthy_buyer_id(fallback_buyer_id) and str(send_user_id) != str(fallback_buyer_id):
                            logger.warning(
                                f'[{msg_time}] 【{self.account_id}】❌ sid兜底命中订单但买家不一致，已拒绝发货: '
                                f'send_user_id={send_user_id}, order_buyer_id={fallback_buyer_id}, sid={fallback_sid}'
                            )
                            return

                        if item_id and item_id != "未知商品" and fallback_item_id and str(item_id) != str(fallback_item_id):
                            logger.warning(
                                f'[{msg_time}] 【{self.account_id}】❌ sid兜底命中订单但商品不一致，已拒绝发货: '
                                f'message_item_id={item_id}, order_item_id={fallback_item_id}, sid={fallback_sid}'
                            )
                            return

                        order_id = fallback_order_id
                        if (not item_id or item_id == "未知商品") and fallback_item_id:
                            item_id = fallback_item_id

                        if sid_match_type == 'bargain_ready':
                            logger.info(
                                f'[{msg_time}] 【{self.account_id}】✅ 订单ID提取失败，但检测到小刀成功证据，'
                                f'使用sid兜底直接进入自动发货: sid={fallback_sid}, order_id={order_id}'
                            )

                        logger.info(
                            f'[{msg_time}] 【{self.account_id}】✅ 订单ID提取失败，已通过sid兜底定位订单: '
                            f'sid={fallback_sid}, order_id={order_id}, item_id={item_id}'
                        )
                    elif recent_order:
                        fallback_order_id = recent_order.get('order_id')
                        fallback_status = recent_order.get('order_status') or 'unknown'
                        if sid_match_type == 'already_processed':
                            logger.info(
                                f'[{msg_time}] 【{self.account_id}】ℹ️ 订单ID提取失败，但sid命中的订单已处理完成，跳过重复发货: '
                                f'sid={fallback_sid}, order_id={fallback_order_id}, status={fallback_status}'
                            )
                        elif sid_match_type == 'cancelled':
                            logger.info(
                                f'[{msg_time}] 【{self.account_id}】ℹ️ 订单ID提取失败，但sid命中的订单已关闭，跳过自动发货: '
                                f'sid={fallback_sid}, order_id={fallback_order_id}'
                            )
                        else:
                            logger.info(
                                f'[{msg_time}] 【{self.account_id}】ℹ️ 订单ID提取失败，但sid命中的订单当前状态不适合兜底发货，等待后续完整消息: '
                                f'sid={fallback_sid}, order_id={fallback_order_id}, status={fallback_status}'
                            )
                        return
                    elif sid_match_type.startswith('ambiguous_'):
                        logger.warning(
                            f'[{msg_time}] 【{self.account_id}】❌ sid兜底命中多个候选订单，严格模式拒绝自动发货: '
                            f'sid={fallback_sid}, match_type={sid_match_type}'
                        )
                        self._record_delivery_log(
                            item_id=item_id,
                            buyer_id=send_user_id,
                            buyer_nick=send_user_name,
                            status='failed',
                            reason=f'sid命中多个候选订单，已拒绝兜底发货: sid={fallback_sid}',
                            channel='auto'
                        )
                        return
                    else:
                        logger.warning(
                            f'[{msg_time}] 【{self.account_id}】❌ 未能提取到订单ID，sid兜底也未命中待发货订单，跳过自动发货 '
                            f'(sid={fallback_sid})'
                        )
                        self._record_delivery_log(
                            item_id=item_id,
                            buyer_id=send_user_id,
                            buyer_nick=send_user_name,
                            status='failed',
                            reason=f'未能提取订单ID且sid未命中待发货订单: sid={fallback_sid}',
                            channel='auto'
                        )
                        return
                else:
                    logger.warning(f'[{msg_time}] 【{self.account_id}】❌ 未能提取到订单ID且无可用sid，跳过自动发货')
                    self._record_delivery_log(
                        item_id=item_id,
                        buyer_id=send_user_id,
                        buyer_nick=send_user_name,
                        status='failed',
                        reason='未能提取到订单ID且无可用sid，跳过自动发',
                        channel='auto'
                    )
                    return

            try:
                existing_order = db_manager.get_order_by_id(order_id, account_id=current_account_id)
            except Exception as order_check_e:
                logger.error(f'[{msg_time}] 【{self.account_id}】查询订单一致性校验失败: {self._safe_str(order_check_e)}')
                existing_order = None

            if not existing_order:
                logger.warning(
                    f'[{msg_time}] 【{current_account_id}】订单未验证归属，拒绝自动发货 order_id={order_id}'
                )
                self._record_delivery_log(
                    order_id=order_id,
                    item_id=item_id,
                    buyer_id=send_user_id,
                    buyer_nick=send_user_name,
                    status='failed',
                    reason='订单未验证归属，拒绝自动发货',
                    channel='auto'
                )
                return

            existing_buyer_id = existing_order.get('buyer_id')
            existing_item_id = existing_order.get('item_id')

            if send_user_id and existing_buyer_id and self._is_trustworthy_buyer_id(existing_buyer_id) and str(send_user_id) != str(existing_buyer_id):
                logger.warning(
                    f'[{msg_time}] 【{self.account_id}】❌ 订单与当前会话买家不丢致，拒绝自动发货: '
                    f'order_id={order_id}, send_user_id={send_user_id}, order_buyer_id={existing_buyer_id}'
                )
                self._record_delivery_log(
                    order_id=order_id,
                    item_id=item_id,
                    buyer_id=send_user_id,
                    buyer_nick=send_user_name,
                    status='failed',
                    reason='订单与当前会话买家不丢致，拒绝自动发货',
                    channel='auto'
                )
                return

            if item_id and item_id != "未知商品" and existing_item_id and str(item_id) != str(existing_item_id):
                logger.warning(
                    f'[{msg_time}] 【{self.account_id}】❌ 订单与当前会话商品不丢致，拒绝自动发货: '
                    f'order_id={order_id}, message_item_id={item_id}, order_item_id={existing_item_id}'
                )
                self._record_delivery_log(
                    order_id=order_id,
                    item_id=item_id,
                    buyer_id=send_user_id,
                    buyer_nick=send_user_name,
                    status='failed',
                    reason='订单与当前会话商品不丢致，拒绝自动发货',
                    channel='auto'
                )
                return

            if (not item_id or item_id == "未知商品") and existing_item_id:
                item_id = existing_item_id
                logger.info(f'[{msg_time}] 【{self.account_id}】订单一致校验补全商品ID: {item_id}')

            if item_id and item_id != "未知商品":
                try:
                    if not await self._ensure_item_owned_by_current_account(
                        item_id,
                        log_prefix=f'[{msg_time}] 【{self.account_id}】'
                    ):
                        logger.warning(f'[{msg_time}] 【{self.account_id}】❌ 商品 {item_id} 不属于当前账号，跳过自动发货')
                        self._record_delivery_log(
                            item_id=item_id,
                            buyer_id=send_user_id,
                            buyer_nick=send_user_name,
                            status='failed',
                            reason='商品不属于当前账号，跳过自动发货',
                            channel='auto'
                        )
                        return
                    logger.warning(f'[{msg_time}] 【{self.account_id}】✅ 商品 {item_id} 归属验证通过')
                except Exception as e:
                    logger.error(f'[{msg_time}] 【{self.account_id}】检查商品归属失败 {self._safe_str(e)}，跳过自动发货')
                    self._record_delivery_log(
                        item_id=item_id,
                        buyer_id=send_user_id,
                        buyer_nick=send_user_name,
                        status='failed',
                        reason=f'检查商品归属失败 {self._safe_str(e)}',
                        channel='auto'
                    )
                    return

            logger.info(f'[{msg_time}] 【{self.account_id}】提取到订单ID: {order_id}，将在自动发货时处理确认发货')

            lock_key = self._compose_order_delivery_scope_key(current_account_id, order_id)
            if not lock_key:
                logger.error(
                    f"【{current_account_id or 'default'}】自动发货缺少有效订单锁作用域，拒绝继续运行: {order_id}"
                )
                return

            if self.is_lock_held(lock_key):
                logger.info(f'[{msg_time}] 【{self.account_id}】🔒【提前检查】订单 {lock_key} 延迟锁仍在持有状态，跳过发货')
                self._record_delivery_log(
                    order_id=order_id,
                    item_id=item_id,
                    buyer_id=send_user_id,
                    buyer_nick=send_user_name,
                    status='failed',
                    reason='订单延迟锁持有中，跳过发',
                    channel='auto'
                )
                return

            if not self.can_auto_delivery(order_id):
                logger.info(f'[{msg_time}] 【{self.account_id}】订单 {order_id} 在冷却期内，跳过发货')
                self._record_delivery_log(
                    order_id=order_id,
                    item_id=item_id,
                    buyer_id=send_user_id,
                    buyer_nick=send_user_name,
                    status='failed',
                    reason='订单在冷却期内，跳过发货',
                    channel='auto'
                )
                return

            order_lock = self._order_locks[lock_key]

            self._lock_usage_times[lock_key] = time.time()

            async with order_lock:
                logger.info(f'[{msg_time}] 【{self.account_id}】获取订单锁成功: {lock_key}，开始处理自动发货')

                if self.is_lock_held(lock_key):
                    logger.info(f'[{msg_time}] 【{self.account_id}】订单 {lock_key} 在获取锁后检查发现延迟锁仍持有，跳过发货')
                    self._record_delivery_log(
                        order_id=order_id,
                        item_id=item_id,
                        buyer_id=send_user_id,
                        buyer_nick=send_user_name,
                        status='failed',
                        reason='获取锁后发现延迟锁仍持有，跳过发',
                        channel='auto'
                    )
                    return

                if not self.can_auto_delivery(order_id):
                    logger.info(f'[{msg_time}] 【{self.account_id}】订单 {order_id} 在获取锁后检查发现仍在冷却期，跳过发货')
                    self._record_delivery_log(
                        order_id=order_id,
                        item_id=item_id,
                        buyer_id=send_user_id,
                        buyer_nick=send_user_name,
                        status='failed',
                        reason='获取锁后发现订单仍在冷却期，跳过发货',
                        channel='auto'
                    )
                    return

                user_url = f'https://www.goofish.com/personal?userId={send_user_id}'

                try:
                    item_title = "待获取商品信息"

                    logger.info(f"【{self.account_id}】准备自动发货: item_id={item_id}, item_title={item_title}")

                    from db_manager import db_manager
                    quantity_to_send = 1
                    multi_quantity_delivery = db_manager.get_item_multi_quantity_delivery_status(current_account_id, item_id)

                    if multi_quantity_delivery and order_id:
                        logger.info(f"商品 {item_id} 开启了多数量发货，获取订单详情...")
                        try:
                            order_detail = await self.fetch_order_detail_info(order_id, item_id, send_user_id)
                            if order_detail and order_detail.get('quantity'):
                                try:
                                    order_quantity = int(order_detail['quantity'])
                                    if order_quantity > 1:
                                        quantity_to_send = order_quantity
                                        logger.info(f"从订单详情获取数量: {order_quantity}，将发送 {quantity_to_send} 个卡券")
                                    else:
                                        logger.info(f"订单数量为 {order_quantity}，发送单个卡券")
                                except (ValueError, TypeError):
                                    logger.warning(f"订单数量格式无效: {order_detail.get('quantity')}，发送单个卡券")
                            else:
                                logger.info(f"未获取到订单数量信息，发送单个卡券")
                        except Exception as e:
                            logger.error(f"获取订单详情失败: {self._safe_str(e)}，发送单个卡券")
                    elif not multi_quantity_delivery:
                        logger.info(f"商品 {item_id} 未开启多数量发货，发送单个卡券")
                    else:
                        logger.info(f"无订单ID，发送单个卡券")

                    successful_send_count = 0
                    last_delivery_error = None
                    prepared_units = []

                    for i in range(quantity_to_send):
                        unit_index = i + 1
                        rule_meta = {}
                        try:
                            pending_finalize_meta = self._get_pending_delivery_finalization_meta(order_id, unit_index)
                            if pending_finalize_meta:
                                finalize_result = await self._finalize_delivery_after_send(
                                    delivery_meta=pending_finalize_meta,
                                    order_id=order_id,
                                    item_id=item_id
                                )
                                if not finalize_result.get('success'):
                                    last_delivery_error = finalize_result.get('error') or f"第 {unit_index} 个卡券补完成收尾失败"
                                    self._persist_delivery_finalization_state(
                                        order_id=order_id,
                                        item_id=item_id,
                                        buyer_id=send_user_id,
                                        delivery_meta=pending_finalize_meta,
                                        channel='auto',
                                        status='sent',
                                        last_error=last_delivery_error
                                    )
                                    self._record_delivery_log(
                                        order_id=order_id,
                                        item_id=item_id,
                                        buyer_id=send_user_id,
                                        buyer_nick=send_user_name,
                                        status='failed',
                                        reason=last_delivery_error,
                                        channel='auto',
                                        rule_meta=pending_finalize_meta
                                    )
                                    logger.error(last_delivery_error)
                                    continue

                                self._persist_delivery_finalization_state(
                                    order_id=order_id,
                                    item_id=item_id,
                                    buyer_id=send_user_id,
                                    delivery_meta=pending_finalize_meta,
                                    channel='auto',
                                    status='finalized'
                                )
                                successful_send_count += 1

                                self._record_delivery_log(
                                    order_id=order_id,
                                    item_id=item_id,
                                    buyer_id=send_user_id,
                                    buyer_nick=send_user_name,
                                    status='success',
                                    reason='棢测到发货消息已发送，本次补完成收尾成',
                                    channel='auto',
                                    rule_meta=pending_finalize_meta
                                )
                                continue

                            delivery_result = await self._auto_delivery(
                                item_id,
                                item_title,
                                order_id,
                                send_user_id,
                                chat_id,
                                send_user_name,
                                include_meta=True,
                                delivery_unit_index=unit_index
                            )

                            if isinstance(delivery_result, dict):
                                delivery_content = delivery_result.get('content')
                                delivery_error = delivery_result.get('error')
                                delivery_steps = delivery_result.get('delivery_steps') or []
                                rule_meta = {
                                    'success': True,
                                    'rule_id': delivery_result.get('rule_id'),
                                    'rule_keyword': delivery_result.get('rule_keyword'),
                                    'card_type': delivery_result.get('card_type'),
                                    'match_mode': delivery_result.get('match_mode'),
                                    'order_spec_mode': delivery_result.get('order_spec_mode'),
                                    'rule_spec_mode': delivery_result.get('rule_spec_mode'),
                                    'item_config_mode': delivery_result.get('item_config_mode'),
                                    'card_id': delivery_result.get('card_id'),
                                    'card_description': delivery_result.get('card_description'),
                                    'data_card_pending_consume': delivery_result.get('data_card_pending_consume'),
                                    'data_line': delivery_result.get('data_line'),
                                    'data_reservation_id': delivery_result.get('data_reservation_id'),
                                    'data_reservation_status': delivery_result.get('data_reservation_status'),
                                    'delivery_unit_index': delivery_result.get('delivery_unit_index')
                                }
                            else:
                                delivery_content = delivery_result
                                delivery_error = None
                                delivery_steps = []

                            if not delivery_content:
                                failure_reason = delivery_error or f"第 {unit_index}/{quantity_to_send} 个卡券内容获取失败"
                                last_delivery_error = failure_reason
                                self._record_delivery_log(
                                    order_id=order_id,
                                    item_id=item_id,
                                    buyer_id=send_user_id,
                                    buyer_nick=send_user_name,
                                    status='failed',
                                    reason=failure_reason,
                                    channel='auto',
                                    rule_meta=rule_meta
                                )
                                logger.warning(failure_reason)
                                continue

                            if not delivery_steps:
                                delivery_steps = self._build_delivery_steps(delivery_content, rule_meta.get('card_description', ''))
                            if not delivery_steps:
                                failure_reason = f"第 {unit_index}/{quantity_to_send} 个卡券发货步骤构建失败"
                                last_delivery_error = failure_reason
                                self._release_data_reservation_if_needed(rule_meta, error=failure_reason)
                                self._record_delivery_log(
                                    order_id=order_id,
                                    item_id=item_id,
                                    buyer_id=send_user_id,
                                    buyer_nick=send_user_name,
                                    status='failed',
                                    reason=failure_reason,
                                    channel='auto',
                                    rule_meta=rule_meta
                                )
                                logger.error(failure_reason)
                                continue

                            prepared_units.append({
                                'unit_index': unit_index,
                                'delivery_steps': delivery_steps,
                                'rule_meta': rule_meta,
                                'card_type': rule_meta.get('card_type'),
                            })

                        except Exception as e:
                            self._release_data_reservation_if_needed(rule_meta, error=f'准备发货失败: {self._safe_str(e)}')
                            last_delivery_error = f"准备第 {unit_index}/{quantity_to_send} 个卡券失败: {self._safe_str(e)}"
                            self._record_delivery_log(
                                order_id=order_id,
                                item_id=item_id,
                                buyer_id=send_user_id,
                                buyer_nick=send_user_name,
                                status='failed',
                                reason=last_delivery_error,
                                channel='auto',
                                rule_meta=rule_meta
                            )
                            logger.error(last_delivery_error)

                    send_groups = self._build_delivery_send_groups(prepared_units, quantity_to_send)
                    total_send_groups = len(send_groups)

                    for group_index, send_group in enumerate(send_groups, start=1):
                        group_units = send_group.get('units') or []
                        if not group_units:
                            continue

                        first_unit = group_units[0]
                        single_unit_index = first_unit.get('unit_index') or 1
                        is_batched_text_group = send_group.get('mode') == 'batched_text'

                        if is_batched_text_group:
                            group_log_prefix = (
                                f'[{msg_time}] 多数量自动发货批次 {group_index}/{total_send_groups} '
                                f'({len(group_units)}个单元, {send_group.get("char_count", 0)}字)'
                            )
                        else:
                            group_log_prefix = f'[{msg_time}] 多数量自动发货 {single_unit_index}/{quantity_to_send}'

                        try:
                            await self._send_delivery_steps(
                                websocket,
                                chat_id,
                                send_user_id,
                                send_group.get('delivery_steps') or [],
                                user_url=user_url,
                                log_prefix=group_log_prefix
                            )
                        except Exception as e:
                            group_error = self._safe_str(e)
                            for prepared_unit in group_units:
                                unit_rule_meta = prepared_unit.get('rule_meta') or {}
                                unit_index = prepared_unit.get('unit_index') or 1
                                self._release_data_reservation_if_needed(
                                    unit_rule_meta,
                                    error=f'发送失败(unit={unit_index}): {group_error}'
                                )
                                last_delivery_error = f"发送第 {unit_index}/{quantity_to_send} 个卡券失败: {group_error}"
                                self._record_delivery_log(
                                    order_id=order_id,
                                    item_id=item_id,
                                    buyer_id=send_user_id,
                                    buyer_nick=send_user_name,
                                    status='failed',
                                    reason=last_delivery_error,
                                    channel='auto',
                                    rule_meta=unit_rule_meta
                                )
                                logger.error(last_delivery_error)
                            continue

                        for prepared_unit in group_units:
                            unit_rule_meta = prepared_unit.get('rule_meta') or {}
                            unit_index = prepared_unit.get('unit_index') or 1
                            unit_delivery_steps = prepared_unit.get('delivery_steps') or []

                            try:
                                if not self._mark_data_reservation_sent_if_needed(unit_rule_meta):
                                    self._release_data_reservation_if_needed(
                                        unit_rule_meta,
                                        error=f'发送成功后标记预占已发送失败(unit={unit_index})'
                                    )
                                    last_delivery_error = f'第 {unit_index} 个卡券发送成功后标记预占已发送失败'
                                    self._record_delivery_log(
                                        order_id=order_id,
                                        item_id=item_id,
                                        buyer_id=send_user_id,
                                        buyer_nick=send_user_name,
                                        status='failed',
                                        reason=last_delivery_error,
                                        channel='auto',
                                        rule_meta=unit_rule_meta
                                    )
                                    logger.error(last_delivery_error)
                                    continue

                                self._persist_delivery_finalization_state(
                                    order_id=order_id,
                                    item_id=item_id,
                                    buyer_id=send_user_id,
                                    delivery_meta=unit_rule_meta,
                                    channel='auto',
                                    status='sent'
                                )

                                finalize_result = await self._finalize_delivery_after_send(
                                    delivery_meta=unit_rule_meta,
                                    order_id=order_id,
                                    item_id=item_id
                                )
                                if not finalize_result.get('success'):
                                    last_delivery_error = finalize_result.get('error') or f"第 {unit_index} 条消息发送成功但提交发货副作用失败"
                                    self._persist_delivery_finalization_state(
                                        order_id=order_id,
                                        item_id=item_id,
                                        buyer_id=send_user_id,
                                        delivery_meta=unit_rule_meta,
                                        channel='auto',
                                        status='sent',
                                        last_error=last_delivery_error
                                    )
                                    self._record_delivery_log(
                                        order_id=order_id,
                                        item_id=item_id,
                                        buyer_id=send_user_id,
                                        buyer_nick=send_user_name,
                                        status='failed',
                                        reason=last_delivery_error,
                                        channel='auto',
                                        rule_meta=unit_rule_meta
                                    )
                                    logger.error(last_delivery_error)
                                    continue

                                self._persist_delivery_finalization_state(
                                    order_id=order_id,
                                    item_id=item_id,
                                    buyer_id=send_user_id,
                                    delivery_meta=unit_rule_meta,
                                    channel='auto',
                                    status='finalized'
                                )

                                successful_send_count += 1

                                has_image_step = any(step.get('type') == 'image' for step in unit_delivery_steps)
                                if has_image_step:
                                    success_reason = '自动发货图片步骤发送成功'
                                elif is_batched_text_group and len(group_units) > 1:
                                    success_reason = '自动发货文本批量合并发送成功'
                                else:
                                    success_reason = '自动发货文本发送成功'

                                self._record_delivery_log(
                                    order_id=order_id,
                                    item_id=item_id,
                                    buyer_id=send_user_id,
                                    buyer_nick=send_user_name,
                                    status='success',
                                    reason=success_reason,
                                    channel='auto',
                                    rule_meta=unit_rule_meta
                                )
                            except Exception as unit_post_error:
                                last_delivery_error = f"第 {unit_index} 个卡券消息已发送，但发送后处理异常: {self._safe_str(unit_post_error)}"
                                self._persist_delivery_finalization_state(
                                    order_id=order_id,
                                    item_id=item_id,
                                    buyer_id=send_user_id,
                                    delivery_meta=unit_rule_meta,
                                    channel='auto',
                                    status='sent',
                                    last_error=last_delivery_error
                                )
                                self._record_delivery_log(
                                    order_id=order_id,
                                    item_id=item_id,
                                    buyer_id=send_user_id,
                                    buyer_nick=send_user_name,
                                    status='failed',
                                    reason=last_delivery_error,
                                    channel='auto',
                                    rule_meta=unit_rule_meta
                                )
                                logger.error(last_delivery_error)

                        if total_send_groups > 1 and group_index < total_send_groups:
                            await asyncio.sleep(1)

                    progress_summary = self._sync_order_delivery_progress(
                        order_id=order_id,
                        account_id=current_account_id,
                        expected_quantity=quantity_to_send,
                        context="自动发货进度同步"
                    ) if order_id else None

                    if progress_summary and progress_summary.get('aggregate_status') in {'partial_success', 'partial_pending_finalize', 'shipped'}:
                        self._activate_delivery_lock(lock_key, delay_minutes=10)

                    if successful_send_count > 0:
                        if progress_summary and quantity_to_send > 1:
                            aggregate_status = progress_summary.get('aggregate_status')
                            finalized_count = progress_summary.get('finalized_count', 0)
                            pending_finalize_count = progress_summary.get('pending_finalize_count', 0)
                            remaining_count = progress_summary.get('remaining_count', 0)

                            if aggregate_status == 'partial_pending_finalize':
                                notify_message = (
                                    f"多数量发货部分完成，已完成 {finalized_count}/{quantity_to_send}，"
                                    f"待收尾 {pending_finalize_count}，待补发 {remaining_count}"
                                )
                            elif aggregate_status == 'partial_success':
                                notify_message = (
                                    f"多数量发货部分成功，已完成 {finalized_count}/{quantity_to_send}，"
                                    f"待补发 {remaining_count}"
                                )
                            else:
                                notify_message = f"多数量发货成功，共完成 {finalized_count}/{quantity_to_send} 个卡券"
                            await self.send_delivery_failure_notification(send_user_name, send_user_id, item_id, notify_message, chat_id)
                        else:
                            await self.send_delivery_failure_notification(send_user_name, send_user_id, item_id, "发货成功", chat_id)
                    else:
                        logger.warning(f'[{msg_time}] 【自动发货未找到匹配的发货规则或获取发货内容失败')
                        self._record_delivery_log(
                            order_id=order_id,
                            item_id=item_id,
                            buyer_id=send_user_id,
                            buyer_nick=send_user_name,
                            status='failed',
                            reason=last_delivery_error or "未找到匹配的发货规则或获取发货内容失败",
                            channel='auto'
                        )
                        await self.send_delivery_failure_notification(send_user_name, send_user_id, item_id, last_delivery_error or "未找到匹配的发货规则或获取发货内容失败", chat_id)

                except Exception as e:
                    self._record_delivery_log(
                        order_id=order_id,
                        item_id=item_id,
                        buyer_id=send_user_id,
                        buyer_nick=send_user_name,
                        status='failed',
                        reason=f"自动发货处理异常: {self._safe_str(e)}",
                        channel='auto'
                    )
                    logger.error(f"自动发货处理异常: {self._safe_str(e)}")
                    await self.send_delivery_failure_notification(send_user_name, send_user_id, item_id, f"自动发货处理异常: {str(e)}", chat_id)

                logger.info(f'[{msg_time}] 【{self.account_id}】订单锁释放: {lock_key}，自动发货处理完成')

        except Exception as e:
            self._record_delivery_log(
                item_id=item_id,
                buyer_id=send_user_id,
                buyer_nick=send_user_name,
                status='failed',
                reason=f"统一自动发货处理异常: {self._safe_str(e)}",
                channel='auto'
            )
            logger.error(f"统一自动发货处理异常: {self._safe_str(e)}")



    def _reload_latest_cookies_from_db(self, reason: str = "") -> bool:
        current_account_id = self._canonical_account_id()
        log_account_id = current_account_id or "default"
        try:
            from db_manager import db_manager
            if not current_account_id:
                logger.warning("【default】从数据库重载Cookie缺少 canonical account_id，跳过从数据库重载Cookie")
                return False

            account_info = db_manager.get_cookie_details(current_account_id)
            new_cookies_str = self._extract_cookie_value(account_info)
            if new_cookies_str and new_cookies_str != self.cookies_str:
                suffix = f" ({reason})" if reason else ""
                logger.info(f"【{log_account_id}】检测到数据库中的cookie已更新，重新加载cookie{suffix}")
                self._set_runtime_cookie_state(cookies_str=new_cookies_str, source=f"db_reload{suffix}")
                logger.warning(f"【{log_account_id}】Cookie已从数据库重新加载")
                return True
        except Exception as reload_e:
            logger.warning(f"【{log_account_id}】从数据库重新加载cookie失败，继续使用当前cookie: {self._safe_str(reload_e)}")
        return False

    def _serialize_cookies(self, cookies_dict: Optional[Dict[str, Any]] = None) -> str:
        cookies = cookies_dict or self.cookies
        return '; '.join([f"{k}={v}" for k, v in cookies.items() if k])

    def _sync_session_cookie_header(self):
        if self.session and not self.session.closed:
            self.session.headers['cookie'] = self.cookies_str

    def _set_runtime_cookie_state(
        self,
        cookies_str: Optional[str] = None,
        cookies_dict: Optional[Dict[str, Any]] = None,
        source: str = "runtime_update",
    ) -> bool:
        normalized_cookies = dict(cookies_dict or trans_cookies(cookies_str or ""))
        if not normalized_cookies:
            logger.warning(f"【{self.account_id}】忽略空Cookie更新: source={source}")
            return False

        previous_cookie_string = self.cookies_str
        previous_unb = self.cookies.get('unb') if isinstance(self.cookies, dict) else None

        self.cookies = normalized_cookies
        self.cookies_str = self._serialize_cookies(normalized_cookies)

        new_unb = self.cookies.get('unb')
        if new_unb and new_unb != previous_unb:
            logger.warning(f"【{self.account_id}】Cookie中的unb发生变化: {previous_unb} -> {new_unb} (source={source})")
            self.myid = new_unb
            self.device_id = generate_device_id(self.myid)

        self._sync_session_cookie_header()
        return self.cookies_str != previous_cookie_string

    async def _persist_runtime_cookie_state(
        self,
        cookies_str: Optional[str] = None,
        cookies_dict: Optional[Dict[str, Any]] = None,
        source: str = "runtime_update",
    ) -> bool:
        changed = self._set_runtime_cookie_state(
            cookies_str=cookies_str,
            cookies_dict=cookies_dict,
            source=source,
        )
        if changed:
            await self.update_config_cookies()
        return changed

    def _extract_set_cookie_updates(self, response_headers) -> Dict[str, str]:
        if not response_headers:
            return {}

        set_cookie_values = []
        try:
            if hasattr(response_headers, 'getall') and 'set-cookie' in response_headers:
                set_cookie_values = response_headers.getall('set-cookie', [])
            elif hasattr(response_headers, 'get_all'):
                set_cookie_values = response_headers.get_all('set-cookie', [])
            elif isinstance(response_headers, dict):
                raw_value = response_headers.get('set-cookie') or response_headers.get('Set-Cookie')
                if isinstance(raw_value, list):
                    set_cookie_values = raw_value
                elif raw_value:
                    set_cookie_values = [raw_value]
        except Exception:
            set_cookie_values = []

        updates = {}
        for cookie in set_cookie_values:
            if '=' not in cookie:
                continue
            name, value = cookie.split(';')[0].split('=', 1)
            updates[name.strip()] = value.strip()
        return updates

    async def _apply_response_cookie_updates(self, response_headers, source: str) -> bool:
        updates = self._extract_set_cookie_updates(response_headers)
        if not updates:
            return False

        merged_cookies = dict(self.cookies)
        merged_cookies.update(updates)
        changed = await self._persist_runtime_cookie_state(
            cookies_dict=merged_cookies,
            source=source,
        )
        if changed:
            logger.info(f"【{self.account_id}】已应用 {len(updates)} 个响应Cookie更新: source={source}")
        return changed

    def _build_websocket_headers(self) -> Dict[str, str]:
        headers = WEBSOCKET_HEADERS.copy()
        headers['Cookie'] = self.cookies_str
        return headers

    def _mark_slider_success_recovery(self, cookies_str: str = ""):
        self.last_slider_success_at = time.time()
        self.last_slider_success_cookie_length = len(cookies_str or "")

    def _build_cookie_string_with_updates(self, base_cookie_string: str = None, updated_cookies: Optional[Dict[str, Any]] = None) -> str:
        merged_cookies = trans_cookies(base_cookie_string or self.cookies_str)
        for key, value in (updated_cookies or {}).items():
            if key:
                merged_cookies[str(key).strip()] = str(value)
        return self._serialize_cookies(merged_cookies)

    def _mark_pending_slider_success_notice(self, source: str = "token_refresh"):
        self.pending_slider_success_notice = {
            'source': source,
            'timestamp': time.time(),
        }

    def _consume_pending_slider_success_notice(self, max_age_seconds: int = 180) -> Optional[Dict[str, Any]]:
        notice = self.pending_slider_success_notice
        self.pending_slider_success_notice = None
        if not notice:
            return None

        notice_timestamp = float(notice.get('timestamp') or 0)
        if notice_timestamp and (time.time() - notice_timestamp) <= max_age_seconds:
            return notice

        logger.info(f"【{self.account_id}】检测到过期的滑块成功待发知，已自动丢弃")
        return None

    def _clear_pending_slider_success_notice(self, reason: str = None):
        if self.pending_slider_success_notice:
            suffix = f" ({reason})" if reason else ""
            logger.info(f"【{self.account_id}】已清理滑块成功待发送通知{suffix}")
        self.pending_slider_success_notice = None

    def _build_x5_cookie_snapshot(self, cookie_string: str = None, cookies_dict: dict = None) -> Dict[str, Dict[str, Any]]:
        source_dict = cookies_dict if cookies_dict is not None else trans_cookies(cookie_string or self.cookies_str)
        snapshot = {}
        for key in ('x5sec', 'x5secdata'):
            value = source_dict.get(key)
            snapshot[key] = {
                'present': bool(value),
                'length': len(str(value)) if value else 0,
                'hash': hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:12] if value else None,
            }
        return snapshot

    def _log_x5_cookie_snapshot(self, label: str, cookie_string: str = None, cookies_dict: dict = None):
        snapshot = self._build_x5_cookie_snapshot(cookie_string=cookie_string, cookies_dict=cookies_dict)
        parts = []
        for key, info in snapshot.items():
            if info.get('present'):
                parts.append(f"{key}=存在(len={info['length']}, sha={info['hash']})")
            else:
                parts.append(f"{key}=缺失")
        logger.info(f"【{self.account_id}】{label}: {', '.join(parts)}")

    @classmethod
    def protected_merge_cookie_dicts(cls, existing_cookies_dict, incoming_cookies_dict):
        existing = dict(existing_cookies_dict or {})
        incoming = dict(incoming_cookies_dict or {})
        existing_count = len(existing)
        incoming_count = len(incoming)
        existing_unb = str(existing.get('unb') or '').strip()
        incoming_unb = str(incoming.get('unb') or '').strip()
        account_switched = bool(existing_unb and incoming_unb and existing_unb != incoming_unb)

        if account_switched:
            merged = incoming.copy()
        else:
            merged = existing.copy()
            for key, value in incoming.items():
                merged[key] = value

        updated_fields = []
        changed_fields = []
        new_fields = []
        for key, value in incoming.items():
            old_value = existing.get(key)
            if old_value is None:
                updated_fields.append(f"{key}(新增)")
                new_fields.append(key)
            elif old_value != value:
                updated_fields.append(key)
                changed_fields.append(key)

        would_remove_fields = [key for key in existing.keys() if key not in incoming]
        if account_switched:
            removed_fields = list(would_remove_fields)
            preserved_fields = []
            preserved_protected_fields = []
        else:
            removed_fields = []
            preserved_fields = list(would_remove_fields)
            preserved_protected_fields = [
                key for key in would_remove_fields
                if key in PROTECTED_SESSION_COOKIE_FIELDS and existing.get(key)
            ]

        missing_protected_fields = [
            key for key in PROTECTED_SESSION_COOKIE_FIELDS
            if not merged.get(key)
        ]
        missing_required_fields = [
            key for key in REQUIRED_SESSION_COOKIE_FIELDS
            if not merged.get(key)
        ]
        incoming_missing_protected_fields = [
            key for key in PROTECTED_SESSION_COOKIE_FIELDS
            if not incoming.get(key)
        ]
        incoming_missing_required_fields = [
            key for key in REQUIRED_SESSION_COOKIE_FIELDS
            if not incoming.get(key)
        ]

        return {
            'existing_cookies_dict': existing,
            'incoming_cookies_dict': incoming,
            'merged_cookies_dict': merged,
            'existing_count': existing_count,
            'incoming_count': incoming_count,
            'merged_count': len(merged),
            'updated_fields': updated_fields,
            'changed_fields': changed_fields,
            'new_fields': new_fields,
            'would_remove_fields': would_remove_fields,
            'removed_fields': removed_fields,
            'preserved_fields': preserved_fields,
            'preserved_protected_fields': preserved_protected_fields,
            'missing_protected_fields': missing_protected_fields,
            'missing_required_fields': missing_required_fields,
            'incoming_missing_protected_fields': incoming_missing_protected_fields,
            'incoming_missing_required_fields': incoming_missing_required_fields,
            'account_switched': account_switched,
        }

    def _merge_cookie_dicts(self, incoming_cookies_dict, existing_cookies_dict=None):
        merge_result = self.protected_merge_cookie_dicts(
            existing_cookies_dict if existing_cookies_dict is not None else trans_cookies(self.cookies_str),
            incoming_cookies_dict,
        )
        return (
            merge_result['existing_cookies_dict'],
            merge_result['merged_cookies_dict'],
            merge_result['updated_fields'],
            merge_result['changed_fields'],
            merge_result['new_fields'],
        )

    def _has_business_ready_cookie_shape(self, cookies_dict: Dict[str, str]) -> bool:
        cookies_dict = cookies_dict or {}
        required_without_cna = (
            'unb',
            'sgcookie',
            'cookie2',
            '_m_h5_tk',
            '_m_h5_tk_enc',
            't',
        )
        if not all(cookies_dict.get(key) for key in required_without_cna):
            return False

        return bool(
            cookies_dict.get('_tb_token_')
            or cookies_dict.get('x5sec')
            or cookies_dict.get('x5secdata')
        )

    def _should_accept_business_ready_cookie_handoff(
        self,
        cookies_dict: Dict[str, str],
        *,
        missing_required_fields: Optional[List[str]] = None,
    ) -> bool:
        normalized_missing_required_fields = [
            str(field).strip()
            for field in (missing_required_fields or [])
            if str(field).strip()
        ]
        if normalized_missing_required_fields and any(field != 'cna' for field in normalized_missing_required_fields):
            return False

        return self._has_business_ready_cookie_shape(cookies_dict)

    def _log_protected_merge_event(self, event_name: str, merge_result: Dict[str, Any]):
        if not merge_result:
            return

        protected_preserved_fields = merge_result.get('preserved_protected_fields') or []
        would_remove_fields = merge_result.get('would_remove_fields') or []
        logger.info(
            f"【{self.account_id}】{event_name} "
            f"incoming_count={merge_result.get('incoming_count', 0)} "
            f"existing_count={merge_result.get('existing_count', 0)} "
            f"merged_count={merge_result.get('merged_count', 0)} "
            f"protected_preserved_fields={protected_preserved_fields} "
            f"would_remove_fields={would_remove_fields} "
            f"account_switched={merge_result.get('account_switched', False)}"
        )

    def _log_cookie_merge_summary(self, merged_cookies_dict, updated_fields, changed_fields, new_fields, context: str,
                                  preserved_fields=None, preserved_protected_fields=None,
                                  would_remove_fields=None, removed_fields=None,
                                  missing_protected_fields=None, missing_required_fields=None,
                                  incoming_missing_protected_fields=None, account_switched: bool = False):
        context_prefix = f"{context} " if context else ""
        logger.info(f"【{self.account_id}】{context_prefix}合并后cookies包含 {len(merged_cookies_dict)} 个字段")

        if updated_fields:
            logger.info(f"【{self.account_id}】{context_prefix}更新的cookie字段: {', '.join(updated_fields)}")
        else:
            logger.info(f"【{self.account_id}】{context_prefix}没有cookie字段需要更新")

        if account_switched:
            logger.warning(f"【{self.account_id}】{context_prefix}棢测到unb变化，按账号切换处理，不保留旧账号Cookie字段")

        if preserved_protected_fields:
            logger.warning(
                f"【{self.account_id}】{context_prefix}保护性保留关键字段 ({len(preserved_protected_fields)}个): {', '.join(preserved_protected_fields)}"
            )
        if preserved_fields:
            logger.info(
                f"【{self.account_id}】{context_prefix}保留旧Cookie字段 ({len(preserved_fields)}个): {', '.join(preserved_fields)}"
            )
        if would_remove_fields:
            logger.info(
                f"【{self.account_id}】{context_prefix}浏览器快照未返回的旧字段 ({len(would_remove_fields)}个): {', '.join(would_remove_fields)}"
            )
        if removed_fields:
            logger.warning(
                f"【{self.account_id}】{context_prefix}实际移除旧字段 ({len(removed_fields)}个): {', '.join(removed_fields)}"
            )
        if incoming_missing_protected_fields:
            logger.warning(
                f"【{self.account_id}】{context_prefix}新快照缺失的关键字段 ({len(incoming_missing_protected_fields)}个): {', '.join(incoming_missing_protected_fields)}"
            )
        if missing_protected_fields:
            logger.warning(
                f"【{self.account_id}】{context_prefix}合并后仍缺失的受保护字段 ({len(missing_protected_fields)}个): {', '.join(missing_protected_fields)}"
            )
        if missing_required_fields:
            logger.error(
                f"【{self.account_id}】{context_prefix}合并后仍缺失的核心字段 ({len(missing_required_fields)}个): {', '.join(missing_required_fields)}"
            )

        important_keys = list(PROTECTED_SESSION_COOKIE_FIELDS) + ['x5sec', 'x5secdata']
        logger.info(f"【{self.account_id}】{context_prefix}关键字段检查:")
        for key in important_keys:
            if key in merged_cookies_dict:
                val = merged_cookies_dict[key]
                marker = " [已变化]" if key in changed_fields else " [新增]" if key in new_fields else ""
                logger.info(f"【{self.account_id}】  ✅ {key}: {'存在' if val else '为空'} (长度: {len(str(val)) if val else 0}){marker}")
            else:
                logger.info(f"【{self.account_id}】  ❌ {key}: 缺失")

    def _has_recent_slider_success(self, window_seconds: int = None) -> bool:
        if not self.last_slider_success_at:
            return False
        window = window_seconds or self.slider_success_reentry_window
        return (time.time() - self.last_slider_success_at) <= window

    async def _preflight_token_for_fresh_auth_cookies(self, label: str, token_source: str) -> str:
        current_account_id = self._canonical_account_id()
        log_account_id = current_account_id or "default"
        if not current_account_id:
            self.last_token_refresh_status = "missing_account_id"
            self.last_token_refresh_error_message = "missing canonical account_id for token preflight"
            logger.error(f"【{log_account_id}】{label}缺少 canonical account_id，拒绝继续运行")
            return ""
        logger.info(f"【{log_account_id}】开始执行{label}...")
        self.last_message_received_time = 0
        previous_skip_reload_flag = bool(getattr(self, '_skip_db_cookie_reload_for_token_refresh', False))
        self._skip_db_cookie_reload_for_token_refresh = True
        try:
            max_preflight_retries = 3
            for attempt in range(1, max_preflight_retries + 1):
                token = await self.refresh_token(allow_password_login_recovery=False)
                if token:
                    self.cache_auth_prewarmed_token(current_account_id, token, source=token_source)
                    logger.info(f"【{log_account_id}】{label}成功（第{attempt}次），已缓存预热token供新实例复用")
                    return token

                if attempt < max_preflight_retries:
                    wait_secs = 2.0 * attempt
                    logger.warning(
                        f"【{log_account_id}】{label}第{attempt}次失败（状态: {self.last_token_refresh_status}），"
                        f"等待{wait_secs:.0f}秒后重试（Cookie可能尚未在服务端生效）"
                    )
                    await asyncio.sleep(wait_secs)
        finally:
            self._skip_db_cookie_reload_for_token_refresh = previous_skip_reload_flag

        raise InitAuthError(f"{label}失败，状态: {self.last_token_refresh_status or 'unknown'}")

    async def preflight_token_after_manual_refresh(self) -> str:
        return await self._preflight_token_for_fresh_auth_cookies(
            label='手动刷新后的Token预检',
            token_source='manual_refresh_handoff',
        )

    async def preflight_token_after_password_login(self) -> str:
        return await self._preflight_token_for_fresh_auth_cookies(
            label='密码登录后的Token预检',
            token_source='password_login_refresh',
        )

    async def refresh_token(self, captcha_retry_count: int = 0, allow_password_login_recovery: bool = True):
        if self.token_refresh_lock.locked():
            logger.info(f"【{self.account_id}】Token刷新已有执行中任务，等待当前流程完成后复用结果")

        async with self.token_refresh_lock:
            if (
                captcha_retry_count == 0 and
                self.current_token and
                self.last_token_refresh_status == "success" and
                (time.time() - self.last_token_refresh_time) < 15
            ):
                logger.info(f"【{self.account_id}】最近15秒内已有成功的Token刷新结果，直接复用当前Token")
                return self.current_token
            return await self._refresh_token_impl(
                captcha_retry_count,
                allow_password_login_recovery=allow_password_login_recovery,
            )

    async def debug_force_captcha_recovery(
        self,
        verification_url: str,
        allow_password_login_recovery: bool = True,
    ) -> Dict[str, Any]:
        current_account_id = self._canonical_account_id()
        log_account_id = current_account_id or "default"
        if not current_account_id:
            logger.error(f"【{log_account_id}】调试模拟滑块恢复缺少 canonical account_id")
            return {
                "success": False,
                "path": "missing_account_id",
                "token_received": False,
                "slider_success": False,
                "password_login_recovered": False,
                "last_token_refresh_status": "missing_account_id",
                "last_token_refresh_error_message": "missing canonical account_id for debug captcha recovery",
            }

        target_verification_url = str(verification_url or "").strip()
        if not target_verification_url:
            target_verification_url = (
                "https://h5api.m.goofish.com/mtop.taobao.idlemessage.pc.login.token/"
                f"punish?x5step=2&action=captcha&pureCaptcha=true&x5secdata=debug_{current_account_id}"
            )

        logger.warning(
            f"【{log_account_id}】开始调试模拟 Token 刷新命中滑块: "
            f"verification_url={target_verification_url}"
        )
        self.last_token_refresh_status = "debug_simulated_captcha_started"
        self.last_token_refresh_error_message = None

        fake_res_json = {
            "ret": ["FAIL_SYS_USER_VALIDATE::DEBUG_SIMULATED_CAPTCHA"],
            "data": {
                "url": target_verification_url,
            },
        }
        risk_session_id = self._new_risk_session_id("slider")
        slider_error_message = None

        try:
            new_cookies_str = await self._handle_captcha_verification(fake_res_json)
            if new_cookies_str:
                logger.info(f"【{log_account_id}】调试模拟滑块链路返回新 Cookie，准备继续刷新 Token")
                await self._restart_instance()
                settle_delay = random.uniform(*self.post_slider_token_retry_delay)
                await asyncio.sleep(settle_delay)
                self._reload_latest_cookies_from_db("debug_simulated_captcha_success")
                token = await self.refresh_token(
                    allow_password_login_recovery=allow_password_login_recovery,
                )
                return {
                    "success": bool(token),
                    "path": "simulated_captcha_slider_success",
                    "token_received": bool(token),
                    "slider_success": True,
                    "password_login_recovered": False,
                    "verification_url": target_verification_url,
                    "last_token_refresh_status": self.last_token_refresh_status,
                    "last_token_refresh_error_message": self.last_token_refresh_error_message,
                }
            slider_error_message = (
                getattr(self, "last_token_refresh_error_message", None)
                or "slider_verification_returned_empty_cookie"
            )
            logger.warning(f"【{log_account_id}】调试模拟滑块链路未拿到新 Cookie: {slider_error_message}")
        except Exception as debug_captcha_error:
            slider_error_message = self._safe_str(debug_captcha_error)
            logger.error(f"【{log_account_id}】调试模拟滑块链路异常: {slider_error_message}")

        self.last_token_refresh_status = "debug_simulated_captcha_failed"
        self.last_token_refresh_error_message = slider_error_message or "debug simulated captcha failed"

        if allow_password_login_recovery:
            logger.warning(f"【{log_account_id}】调试模拟滑块失败，改走账号密码恢复链路")
            refresh_success = await self._try_password_login_refresh(
                "调试模拟滑块验证失败",
                risk_session_id=risk_session_id,
                trigger_scene="token_refresh_debug",
                ignore_slider_failed_backoff=True,
            )
            if refresh_success:
                token = await self.refresh_token(
                    allow_password_login_recovery=allow_password_login_recovery,
                )
                return {
                    "success": bool(token),
                    "path": "simulated_captcha_password_login_recovery",
                    "token_received": bool(token),
                    "slider_success": False,
                    "password_login_recovered": True,
                    "verification_url": target_verification_url,
                    "last_token_refresh_status": self.last_token_refresh_status,
                    "last_token_refresh_error_message": self.last_token_refresh_error_message,
                }

        return {
            "success": False,
            "path": "simulated_captcha_failed",
            "token_received": False,
            "slider_success": False,
            "password_login_recovered": False,
            "verification_url": target_verification_url,
            "last_token_refresh_status": self.last_token_refresh_status,
            "last_token_refresh_error_message": self.last_token_refresh_error_message,
        }

    def _is_auth_failure_ret(self, ret_value: Any) -> bool:
        if isinstance(ret_value, str):
            ret_text = ret_value
        elif isinstance(ret_value, (list, tuple)):
            ret_text = ' | '.join([str(item) for item in ret_value])
        else:
            ret_text = str(ret_value or '')

        auth_keywords = (
            '令牌过期',
            'session过期',
            'FAIL_SYS_USER_VALIDATE',
            'FAIL_SYS_TOKEN_EXPIRED',
            'FAIL_SYS_TOKEN_EXOIRED',
            'FAIL_SYS_SESSION_EXPIRED',
            'passport.goofish.com',
            'mini_login',
            'login',
        )
        ret_text_lower = ret_text.lower()
        return any(keyword.lower() in ret_text_lower for keyword in auth_keywords)

    async def keep_session_alive(self) -> bool:
        self.last_session_keepalive_status = "started"
        self.last_session_keepalive_error_message = None

        try:
            if not self.session:
                await self.create_session()

            self._reload_latest_cookies_from_db("轻量保活前")

            params = {
                'jsv': '2.7.2',
                'appKey': '34839810',
                't': str(int(time.time() * 1000)),
                'sign': '',
                'v': '1.0',
                'type': 'originaljson',
                'accountSite': 'xianyu',
                'dataType': 'json',
                'timeout': '20000',
                'api': 'mtop.taobao.idlemessage.pc.loginuser.get',
                'sessionOption': 'AutoLoginOnly',
                'spm_cnt': 'a21ybx.im.0.0',
                'spm_pre': 'a21ybx.item.want.1.12523da6waCtUp',
                'log_id': '12523da6waCtUp',
            }
            data_val = '{}'
            data = {'data': data_val}

            token = trans_cookies(self.cookies_str).get('_m_h5_tk', '').split('_')[0] if trans_cookies(self.cookies_str).get('_m_h5_tk') else ''
            params['sign'] = generate_sign(params['t'], token, data_val)

            headers = DEFAULT_HEADERS.copy()
            headers['content-type'] = 'application/x-www-form-urlencoded'
            headers['cookie'] = self.cookies_str

            request_kwargs = {}
            if getattr(self, '_http_proxy_url', None):
                request_kwargs['proxy'] = self._http_proxy_url

            api_url = API_ENDPOINTS.get('login_user')
            async with self.session.post(
                api_url,
                params=params,
                data=data,
                headers=headers,
                **request_kwargs,
            ) as response:
                try:
                    res_json = await response.json(content_type=None)
                except Exception:
                    response_text = await response.text()
                    self.last_session_keepalive_status = "response_parse_failed"
                    self.last_session_keepalive_error_message = response_text[:200]
                    logger.warning(f"【{self.account_id}】轻量保活响应解析失败: {response_text[:200]}")
                    return False

                await self._apply_response_cookie_updates(response.headers, "session_keepalive")
                ret_value = res_json.get('ret', [])
                if any('SUCCESS::调用成功' in str(ret) for ret in ret_value):
                    self.last_session_keepalive_status = "success"
                    self.last_session_keepalive_error_message = None
                    self.last_session_keepalive_time = time.time()
                    logger.info(f"【{self.account_id}】轻量会话保活成功")
                    return True

                error_message = ' | '.join([str(ret) for ret in ret_value]) or '未知错误'
                self.last_session_keepalive_error_message = error_message
                self.last_session_keepalive_status = "auth_failed" if self._is_auth_failure_ret(ret_value) else "api_failed"
                logger.warning(f"【{self.account_id}】轻量会话保活失败: {error_message}")
                return False

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.last_session_keepalive_status = "network_failed"
            self.last_session_keepalive_error_message = self._safe_str(e)
            logger.warning(f"【{self.account_id}】轻量会话保活网络异常: {self._safe_str(e)}")
            return False
        except Exception as e:
            self.last_session_keepalive_status = "exception"
            self.last_session_keepalive_error_message = self._safe_str(e)
            logger.error(f"【{self.account_id}】轻量会话保活异常: {self._safe_str(e)}")
            return False

    async def _refresh_token_impl(self, captcha_retry_count: int = 0, post_slider_session_grace_used: bool = False,
                                  allow_password_login_recovery: bool = True,
                                  manual_refresh_browser_stabilization_used: bool = False,
                                  post_slider_session_retry_count: int = 0):
        notification_sent = False
        current_account_id = self._canonical_account_id()
        log_account_id = current_account_id or "default"
        if not current_account_id:
            logger.error(f"【{log_account_id}】Token刷新缺少 canonical account_id，拒绝继续运行")
            self.last_token_refresh_status = "missing_account_id"
            self.last_token_refresh_error_message = "missing canonical account_id for token refresh"
            return None

        try:
            logger.info(f"【{log_account_id}】开始刷新token... (滑块验证重试次数: {captcha_retry_count})")
            self.last_token_refresh_status = "started"
            self.last_token_refresh_error_message = None
            self.restarted_in_browser_refresh = False

            if captcha_retry_count >= self.max_captcha_verification_count:
                logger.error(f"【{log_account_id}】滑块验证重试次数已达上限 ({self.max_captcha_verification_count})，停止重试")
                self.last_token_refresh_status = "captcha_max_retries_exceeded"
                self._clear_pending_slider_success_notice("滑块重试次数达到上限")
                await self.send_token_refresh_notification(
                    f"滑块验证重试次数已达上限，请手动处理",
                    "captcha_max_retries_exceeded"
                )
                notification_sent = True
                return None

            current_time = time.time()
            time_since_last_message = current_time - self.last_message_received_time
            if self.last_message_received_time > 0 and time_since_last_message < self.message_cookie_refresh_cooldown:
                remaining_time = self.message_cookie_refresh_cooldown - time_since_last_message
                remaining_minutes = int(remaining_time // 60)
                remaining_seconds = int(remaining_time % 60)
                logger.info(f"【{log_account_id}】收到消息后冷却中，放弃本次token刷新，还需等待 {remaining_minutes}分{remaining_seconds}秒")
                self.last_token_refresh_status = "skipped_cooldown"
                return None

            logger.info(f"【{log_account_id}】开始执行Cookie刷新任务...")
            if getattr(self, '_skip_db_cookie_reload_for_token_refresh', False):
                logger.info(f"【{log_account_id}】当前Token刷新使用内存中的新认证Cookie，跳过token刷新前的数据库重载")
            else:
                self._reload_latest_cookies_from_db("token刷新前")

            timestamp = str(int(time.time() * 1000))

            params = {
                'jsv': '2.7.2',
                'appKey': '34839810',
                't': timestamp,
                'sign': '',
                'v': '1.0',
                'type': 'originaljson',
                'accountSite': 'xianyu',
                'dataType': 'json',
                'timeout': '20000',
                'api': 'mtop.taobao.idlemessage.pc.login.token',
                'sessionOption': 'AutoLoginOnly',
                'dangerouslySetWindvaneParams': '%5Bobject%20Object%5D',
                'smToken': 'token',
                'queryToken': 'sm',
                'sm': 'sm',
                'spm_cnt': 'a21ybx.im.0.0',
                'spm_pre': 'a21ybx.home.sidebar.1.4c053da6vYwnmf',
                'log_id': '4c053da6vYwnmf'
            }
            data_val = '{"appKey":"444e9908a51d1cb236a27862abc769c9","deviceId":"' + self.device_id + '"}'
            data = {
                'data': data_val,
            }

            token = trans_cookies(self.cookies_str).get('_m_h5_tk', '').split('_')[0] if trans_cookies(self.cookies_str).get('_m_h5_tk') else ''

            sign = generate_sign(params['t'], token, data_val)
            params['sign'] = sign

            headers = {
                'accept': 'application/json',
                'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'cache-control': 'no-cache',
                'content-type': 'application/x-www-form-urlencoded',
                'pragma': 'no-cache',
                'priority': 'u=1, i',
                'sec-ch-ua': '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Windows"',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-site',
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
                'referer': 'https://www.goofish.com/',
                'origin': 'https://www.goofish.com',
                'cookie': self.cookies_str
            }

            api_url = API_ENDPOINTS.get('token')
            logger.info(f"【{log_account_id}】正在刷新Token... API: {api_url}")

            logger.debug(f"【{log_account_id}】Token刷新参数: timestamp={params['t']}, sign={sign[:16]}...")

            if not self.session:
                await self.create_session()
            request_kwargs = {}
            if getattr(self, '_http_proxy_url', None):
                request_kwargs['proxy'] = self._http_proxy_url
            async with self.session.post(
                    api_url,
                    params=params,
                    data=data,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                    **request_kwargs,
                ) as response:
                    res_json = await response.json(content_type=None)
                    ret_info = res_json.get('ret', [])
                    logger.debug(f"【{log_account_id}】Token刷新响应: status={response.status}, ret={ret_info}")

                    response_set_cookies = self._extract_set_cookie_updates(response.headers)

                    transient_recovery_cookies_str = self.cookies_str
                    if response_set_cookies:
                        transient_recovery_cookies_str = self._build_cookie_string_with_updates(
                            self.cookies_str,
                            response_set_cookies
                        )
                        logger.info(
                            f"【{log_account_id}】Token预检响应携带 {len(response_set_cookies)} 个临时Cookie，"
                            f"仅用于本次恢复链路，不提前写入数据库"
                        )

                    if isinstance(res_json, dict):
                        ret_value = res_json.get('ret', [])
                        if any('SUCCESS::调用成功' in ret for ret in ret_value):
                            if 'data' in res_json and 'accessToken' in res_json['data']:
                                if response_set_cookies:
                                    await self._apply_response_cookie_updates(response.headers, "token_refresh")
                                    logger.warning(f"【{log_account_id}】Token刷新成功后已更新Cookie到数据库")

                                new_token = res_json['data']['accessToken']
                                self.current_token = new_token
                                self.last_token_refresh_time = time.time()

                                self.last_message_received_time = 0
                                logger.warning(f"【{log_account_id}】Token刷新成功，已重置消息接收时间标识")
                                self.clear_qr_login_grace(current_account_id)
                                self.clear_init_auth_failure_state(current_account_id)
                                self.last_init_failure_reason = None
                                self.last_init_failure_type = None
                                self.init_auth_failures = 0

                                logger.info(f"【{log_account_id}】Token刷新成功")
                                self.last_token_refresh_status = "success"
                                self.last_token_refresh_error_message = None
                                if self._consume_pending_slider_success_notice():
                                    await self.send_token_refresh_notification(
                                        "滑块验证通过，账号会话已恢复",
                                        "slider_recovered_success"
                                    )
                                return new_token

                    if self._need_captcha_verification(res_json):
                        qr_login_grace = self.get_qr_login_grace(current_account_id)
                        if qr_login_grace and not qr_login_grace.get('captcha_buffer_used'):
                            logger.warning(f"【{log_account_id}】扫码登录后的首轮Token刷新命中风控，先执行浏览器侧Cookie稳定化")
                            log_captcha_event(
                                current_account_id,
                                "扫码登录首轮Token刷新命中风控，先执行浏览器侧稳定化",
                                None,
                                f"触发场景: Token刷新, ret={res_json.get('ret', [])}"
                            )
                            self.update_qr_login_grace(
                                current_account_id,
                                captcha_buffer_used=True,
                                captcha_detected_at=time.time()
                            )
                            await asyncio.sleep(2)
                            stabilization_success = await self._refresh_cookies_via_browser_page(
                                transient_recovery_cookies_str,
                                restart_on_success=False
                            )
                            if stabilization_success:
                                self.update_qr_login_grace(
                                    current_account_id,
                                    browser_stabilized=True,
                                    browser_stabilized_at=time.time()
                                )
                                logger.info(f"【{log_account_id}】浏览器侧Cookie稳定化完成，重新尝试Token刷新")
                                return await self._refresh_token_impl(
                                    captcha_retry_count,
                                    post_slider_session_grace_used=post_slider_session_grace_used,
                                    allow_password_login_recovery=allow_password_login_recovery,
                                    manual_refresh_browser_stabilization_used=manual_refresh_browser_stabilization_used,
                                    post_slider_session_retry_count=post_slider_session_retry_count,
                                )
                            logger.warning(f"【{log_account_id}】浏览器侧Cookie稳定化未消除风控，继续进入滑块验证")

                        manual_refresh_state = self.get_manual_refresh_state(current_account_id)
                        is_manual_refresh_handoff = bool(
                            manual_refresh_state and manual_refresh_state.get('phase') == 'handoff_recovery'
                        )
                        if is_manual_refresh_handoff and not manual_refresh_browser_stabilization_used:
                            logger.warning(f"【{log_account_id}】手动刷新交接阶段首轮Token预检命中风控，先执行浏览器侧Cookie稳定化")
                            log_captcha_event(
                                current_account_id,
                                "手动刷新交接阶段首轮Token预检命中风控，先执行浏览器侧稳定化",
                                None,
                                f"触发场景: Token刷新, ret={res_json.get('ret', [])}"
                            )
                            before_x5_snapshot = self._build_x5_cookie_snapshot(cookie_string=transient_recovery_cookies_str)
                            self._log_x5_cookie_snapshot("手动刷新交接稳定化前的x5票据", cookie_string=transient_recovery_cookies_str)
                            self.last_token_refresh_status = "manual_refresh_browser_stabilizing"
                            stabilization_success = await self._refresh_cookies_via_browser_page(
                                transient_recovery_cookies_str,
                                restart_on_success=False
                            )
                            if stabilization_success:
                                self._reload_latest_cookies_from_db("手动刷新交接阶段浏览器稳定化")
                                after_x5_snapshot = self._build_x5_cookie_snapshot()
                                self._log_x5_cookie_snapshot("手动刷新交接稳定化后的x5票据")
                                changed_x5_fields = [
                                    key for key in ('x5sec', 'x5secdata')
                                    if before_x5_snapshot.get(key, {}).get('hash') != after_x5_snapshot.get(key, {}).get('hash')
                                ]
                                if changed_x5_fields:
                                    logger.info(
                                        f"【{log_account_id}】手动刷新交接阶段浏览器稳定化已更新x5票据: {', '.join(changed_x5_fields)}"
                                    )
                                else:
                                    logger.info(f"【{log_account_id}】手动刷新交接阶段浏览器稳定化未观察到x5票据变化，继续重试Token预检")
                                return await self._refresh_token_impl(
                                    captcha_retry_count,
                                    post_slider_session_grace_used=post_slider_session_grace_used,
                                    allow_password_login_recovery=allow_password_login_recovery,
                                    manual_refresh_browser_stabilization_used=True,
                                    post_slider_session_retry_count=post_slider_session_retry_count,
                                )
                            logger.warning(f"【{log_account_id}】手动刷新交接阶段浏览器稳定化失败，继续进入滑块验证")

                        if self.is_manual_refresh_active(current_account_id, allow_handoff_recovery=True):
                            logger.warning(f"【{log_account_id}】检测到手动刷新进行中，跳过自动滑块处理")
                            log_captcha_event(
                                current_account_id,
                                "手动刷新进行中，跳过自动滑块处理",
                                None,
                                "触发场景: Token刷新"
                            )
                            self.last_token_refresh_status = "manual_refresh_active"
                            self._clear_pending_slider_success_notice("手动刷新进行中")
                            notification_sent = True
                            return None

                        logger.warning(f"【{log_account_id}】检测到需要滑块验证，开始处理...")

                        verification_url = res_json.get('data', {}).get('url', 'Token刷新时检测')
                        log_captcha_event(current_account_id, "检测到滑块验证", None, f"触发场景: Token刷新, URL: {verification_url}")
                        captcha_trigger_scene = 'token_refresh'
                        captcha_session_id = self._new_risk_session_id('slider')
                        captcha_event_meta = self._build_risk_event_meta(
                            trigger_scene=captcha_trigger_scene,
                            verification_url=verification_url,
                            extra={'account_id': current_account_id}
                        )

                        log_id = None
                        try:
                            log_id = self._create_risk_log(
                                event_type='slider_captcha',
                                session_id=captcha_session_id,
                                trigger_scene=captcha_trigger_scene,
                                result_code='slider_captcha_detected',
                                event_description='检测到滑块验证（Token刷新）',
                                processing_status='processing',
                                event_meta=captcha_event_meta,
                            )
                            if log_id:
                                logger.info(f"【{log_account_id}】风控日志记录成功，ID: {log_id}")
                        except Exception as log_e:
                            logger.error(f"【{log_account_id}】记录风控日志失败: {log_e}")

                        try:
                            captcha_start_time = time.time()
                            new_cookies_str = await self._handle_captcha_verification(res_json)
                            captcha_duration = time.time() - captcha_start_time

                            if new_cookies_str:
                                logger.info(f"【{log_account_id}】滑块验证成功，准备重启实例...")

                                if 'log_id' in locals() and log_id:
                                    self._update_risk_log(
                                        log_id,
                                        session_id=captcha_session_id,
                                        trigger_scene=captcha_trigger_scene,
                                        result_code='slider_captcha_success',
                                        processing_result='滑块验证成功，已获取新Cookie',
                                        processing_status='success',
                                        duration_ms=max(0, int(captcha_duration * 1000)),
                                        event_meta=self._build_risk_event_meta(
                                            trigger_scene=captcha_trigger_scene,
                                            verification_url=verification_url,
                                            extra={
                                                'account_id': current_account_id,
                                                'cookie_length': len(new_cookies_str),
                                            },
                                        ),
                                    )

                                await self._restart_instance()

                                settle_delay = random.uniform(*self.post_slider_token_retry_delay)
                                logger.info(
                                    f"【{log_account_id}】滑块成功后进入稳定窗口 {settle_delay:.2f}s，再重新尝试Token刷新"
                                )
                                await asyncio.sleep(settle_delay)
                                self._reload_latest_cookies_from_db("滑块成功后的稳定窗口")
                                log_captcha_event(
                                    current_account_id,
                                    "滑块成功后重新进入Token刷新",
                                    None,
                                    f"类型: token_reentry_after_slider_success, captcha_retry_count={captcha_retry_count + 1}"
                                )

                                return await self._refresh_token_impl(
                                    captcha_retry_count + 1,
                                    post_slider_session_grace_used=False,
                                    allow_password_login_recovery=allow_password_login_recovery,
                                    manual_refresh_browser_stabilization_used=manual_refresh_browser_stabilization_used,
                                    post_slider_session_retry_count=0,
                                )
                            else:
                                logger.error(f"【{log_account_id}】滑块验证失败")

                                if 'log_id' in locals() and log_id:
                                    self._update_risk_log(
                                        log_id,
                                        session_id=captcha_session_id,
                                        trigger_scene=captcha_trigger_scene,
                                        result_code='slider_captcha_failed',
                                        processing_result='滑块验证失败，未获取到新Cookie',
                                        processing_status='failed',
                                        error_message='未获取到新Cookie',
                                        duration_ms=max(0, int(captcha_duration * 1000)),
                                        event_meta=self._build_risk_event_meta(
                                            trigger_scene=captcha_trigger_scene,
                                            verification_url=verification_url,
                                            extra={'account_id': current_account_id},
                                        ),
                                    )

                                if allow_password_login_recovery:
                                    logger.warning(
                                        f"【{log_account_id}】Token刷新滑块链路失败，改走账号密码登录恢复，"
                                        f"verification_url={verification_url}"
                                    )
                                    refresh_success = await self._try_password_login_refresh(
                                        "滑块验证失败",
                                        risk_session_id=captcha_session_id,
                                        trigger_scene=captcha_trigger_scene,
                                    )
                                    if refresh_success:
                                        return await self._refresh_token_impl(
                                            captcha_retry_count,
                                            post_slider_session_grace_used=False,
                                            allow_password_login_recovery=allow_password_login_recovery,
                                            manual_refresh_browser_stabilization_used=manual_refresh_browser_stabilization_used,
                                            post_slider_session_retry_count=0,
                                        )

                                notification_sent = True
                        except Exception as captcha_e:
                            logger.error(f"【{log_account_id}】滑块验证处理异常: {self._safe_str(captcha_e)}")
                            self._clear_pending_slider_success_notice("滑块验证处理异常")

                            captcha_duration = time.time() - captcha_start_time if 'captcha_start_time' in locals() else 0
                            if 'log_id' in locals() and log_id:
                                self._update_risk_log(
                                    log_id,
                                    session_id=captcha_session_id,
                                    trigger_scene=captcha_trigger_scene,
                                    result_code='slider_captcha_exception',
                                    processing_result='滑块验证处理异常',
                                    processing_status='failed',
                                    error_message=str(captcha_e)[:200],
                                    duration_ms=max(0, int(captcha_duration * 1000)),
                                        event_meta=self._build_risk_event_meta(
                                            trigger_scene=captcha_trigger_scene,
                                            verification_url=verification_url,
                                            extra={'account_id': current_account_id},
                                        ),
                                    )

                            notification_sent = True

                    if isinstance(res_json, dict):
                        res_json_str = json.dumps(res_json, ensure_ascii=False, separators=(',', ':'))
                        if '令牌过期' in res_json_str or 'Session过期' in res_json_str:
                            token_expired_log_id = None
                            token_expired_session_id = self._new_risk_session_id('token')
                            token_expired_started_at = time.time()
                            token_trigger_scene = 'token_refresh'
                            expire_type = '令牌过期' if '令牌过期' in res_json_str else 'Session过期'
                            try:
                                from db_manager import db_manager
                                stale_count = db_manager.mark_stale_risk_control_logs_failed(
                                    timeout_minutes=15,
                                    account_id=current_account_id,
                                )
                                if stale_count > 0:
                                    logger.warning(f"【{log_account_id}】检测到{stale_count}条超时processing风控日志，已自动标记failed")
                                token_expired_log_id = self._create_risk_log(
                                    event_type='token_expired',
                                    session_id=token_expired_session_id,
                                    trigger_scene=token_trigger_scene,
                                    result_code='token_expired_detected',
                                    event_description=f"棢测到{expire_type}",
                                    processing_status='processing',
                                    event_meta=self._build_risk_event_meta(
                                        trigger_scene=token_trigger_scene,
                                        extra={'expire_type': expire_type, 'account_id': current_account_id},
                                    ),
                                )
                            except Exception as log_e:
                                logger.error(f"【{log_account_id}】记录风控日志失败: {log_e}")

                            if self.is_manual_refresh_active(current_account_id, allow_handoff_recovery=True):
                                logger.warning(f"【{log_account_id}】检测到手动刷新进行中，跳过自动密码登录刷新")
                                if token_expired_log_id:
                                    self._update_risk_log(
                                        token_expired_log_id,
                                        session_id=token_expired_session_id,
                                        trigger_scene=token_trigger_scene,
                                        result_code='manual_refresh_active',
                                        processing_status='failed',
                                        error_message='棢测到手动刷新进行中，自动刷新已跳',
                                        duration_ms=max(0, int((time.time() - token_expired_started_at) * 1000)),
                                        event_meta=self._build_risk_event_meta(
                                            trigger_scene=token_trigger_scene,
                                            extra={'account_id': current_account_id, 'expire_type': expire_type},
                                        ),
                                    )
                                self.last_token_refresh_status = "manual_refresh_active"
                                self._clear_pending_slider_success_notice("手动刷新进行中")
                                notification_sent = True
                                return None

                            manual_refresh_state = self.get_manual_refresh_state(current_account_id)
                            qr_login_grace = self.get_qr_login_grace(current_account_id)
                            is_handoff_recovery = bool(
                                manual_refresh_state and manual_refresh_state.get('phase') == 'handoff_recovery'
                            ) or bool(
                                qr_login_grace and qr_login_grace.get('stage') == 'real_cookie_ready'
                            )
                            recent_slider_success = self._has_recent_slider_success()
                            max_post_slider_session_retries = 2

                            if is_handoff_recovery and not post_slider_session_grace_used:
                                handoff_grace_delay = random.uniform(1.8, 3.0)
                                logger.warning(
                                    f"【{log_account_id}】检测到账号刚完成Cookie交接，首轮Token刷新先进入交接缓冲窗口，"
                                    f"等待 {handoff_grace_delay:.2f}s 后重载Cookie再重试一次"
                                )
                                log_captcha_event(
                                    current_account_id,
                                    "账号交接首轮Token刷新进入缓冲窗口",
                                    None,
                                    f"类型: handoff_token_retry, expire_type={expire_type}"
                                )
                                await asyncio.sleep(handoff_grace_delay)
                                self._reload_latest_cookies_from_db("账号交接后的Session过期缓冲")
                                return await self._refresh_token_impl(
                                    captcha_retry_count,
                                    post_slider_session_grace_used=True,
                                    allow_password_login_recovery=allow_password_login_recovery,
                                    manual_refresh_browser_stabilization_used=manual_refresh_browser_stabilization_used,
                                    post_slider_session_retry_count=post_slider_session_retry_count,
                                )

                            if recent_slider_success and not post_slider_session_grace_used:
                                grace_delay = random.uniform(1.2, 2.2)
                                logger.warning(
                                    f"【{log_account_id}】检测到最近 {self.slider_success_reentry_window}s 内刚通过滑块，"
                                    f"先等待 {grace_delay:.2f}s 并重载Cookie后再试一次Token刷新"
                                )
                                log_captcha_event(
                                    current_account_id,
                                    "滑块成功后Session过期，优先重试Token刷新",
                                    None,
                                    f"类型: token_retry_after_recent_slider_success, expire_type={expire_type}"
                                )
                                await asyncio.sleep(grace_delay)
                                self._reload_latest_cookies_from_db("滑块成功后的Session过期缓冲")
                                return await self._refresh_token_impl(
                                    captcha_retry_count,
                                    post_slider_session_grace_used=True,
                                    allow_password_login_recovery=allow_password_login_recovery,
                                    manual_refresh_browser_stabilization_used=manual_refresh_browser_stabilization_used,
                                    post_slider_session_retry_count=post_slider_session_retry_count,
                                )

                            if (
                                recent_slider_success and
                                not allow_password_login_recovery and
                                post_slider_session_retry_count < max_post_slider_session_retries
                            ):
                                settle_retry_attempt = post_slider_session_retry_count + 1
                                settle_delay = random.uniform(2.4, 3.6) + ((settle_retry_attempt - 1) * 0.8)
                                logger.warning(
                                    f"【{log_account_id}】预检模式下滑块成功后仍返回{expire_type}，"
                                    f"执行第{settle_retry_attempt}/{max_post_slider_session_retries}次稳定重试，"
                                    f"等待 {settle_delay:.2f}s 后再次尝试Token刷新"
                                )
                                log_captcha_event(
                                    current_account_id,
                                    "滑块成功后Session仍未稳定，继续重试Token刷新",
                                    None,
                                    f"类型: token_settle_retry_after_slider, expire_type={expire_type}, "
                                    f"attempt={settle_retry_attempt}/{max_post_slider_session_retries}"
                                )
                                self.last_token_refresh_status = "post_slider_session_settling"
                                await asyncio.sleep(settle_delay)
                                self._reload_latest_cookies_from_db(
                                    f"滑块成功后的第{settle_retry_attempt}次Session稳定重试"
                                )
                                return await self._refresh_token_impl(
                                    captcha_retry_count,
                                    post_slider_session_grace_used=True,
                                    allow_password_login_recovery=allow_password_login_recovery,
                                    manual_refresh_browser_stabilization_used=manual_refresh_browser_stabilization_used,
                                    post_slider_session_retry_count=settle_retry_attempt,
                                )

                            refresh_success = False
                            if allow_password_login_recovery:
                                refresh_success = await self._try_password_login_refresh(
                                    "令牌/Session过期",
                                    risk_session_id=token_expired_session_id,
                                    trigger_scene=token_trigger_scene,
                                    ignore_slider_failed_backoff=recent_slider_success,
                                )
                            else:
                                self.last_token_refresh_status = (
                                    "session_expired_after_slider"
                                    if recent_slider_success else
                                    "session_expired_preflight"
                                )
                                self.last_token_refresh_error_message = f"Token预检返回{expire_type}"
                                logger.warning(f"【{log_account_id}】当前为预检模式，跳过密码登录恢复，直接返回Token刷新失败")

                            if token_expired_log_id:
                                self._update_risk_log(
                                    token_expired_log_id,
                                    session_id=token_expired_session_id,
                                    trigger_scene=token_trigger_scene,
                                    result_code='token_refresh_recovered' if refresh_success else 'token_refresh_recovery_failed',
                                    processing_status='success' if refresh_success else 'failed',
                                    processing_result='令牌/Session过期触发自动刷新成功，已进入重试流程' if refresh_success else None,
                                    error_message=None if refresh_success else '令牌/Session过期触发自动刷新失败',
                                    duration_ms=max(0, int((time.time() - token_expired_started_at) * 1000)),
                                    event_meta=self._build_risk_event_meta(
                                        trigger_scene=token_trigger_scene,
                                        extra={'account_id': current_account_id, 'expire_type': expire_type},
                                    ),
                                )

                            if not refresh_success:
                                if allow_password_login_recovery:
                                    self.last_token_refresh_status = "token_expired_recovery_failed"
                                self._clear_pending_slider_success_notice("恢复流程失败")
                                notification_sent = True
                                return None
                            else:
                                return await self._refresh_token_impl(
                                    captcha_retry_count,
                                    post_slider_session_grace_used=False,
                                    allow_password_login_recovery=allow_password_login_recovery,
                                    manual_refresh_browser_stabilization_used=manual_refresh_browser_stabilization_used,
                                    post_slider_session_retry_count=0,
                                )


                    if self.last_token_refresh_status in (None, "started"):
                        self.last_token_refresh_status = "token_refresh_failed"
                    self.last_token_refresh_error_message = json.dumps(res_json, ensure_ascii=False, separators=(',', ':'))
                    self._clear_pending_slider_success_notice("Token刷新最终失败")
                    logger.error(f"【{log_account_id}】Token刷新失败: {res_json}")

                    self.current_token = None

                    if not notification_sent:
                        is_ws_connected = (
                            self.connection_state == ConnectionState.CONNECTED and
                            self.ws and
                            not self.ws.closed
                        )

                        if is_ws_connected:
                            logger.info(f"【{log_account_id}】WebSocket连接正常，Token刷新失败可能是暂时的，跳过失败知")
                        else:
                            logger.warning(f"【{log_account_id}】WebSocket未连接，发Token刷新失败通知")
                            await self.send_token_refresh_notification(f"Token刷新失败: {res_json}", "token_refresh_failed")
                    else:
                        logger.info(f"【{log_account_id}】已发滑块验证相关通知，跳过Token刷新失败通知")
                    return None

        except Exception as e:
            self.last_token_refresh_status = "token_refresh_exception"
            self.last_token_refresh_error_message = self._safe_str(e)
            self._clear_pending_slider_success_notice("Token刷新异常")
            logger.error(f"【{log_account_id}】Token刷新异常: {self._safe_str(e)}")

            self.current_token = None

            if not notification_sent:
                is_ws_connected = (
                    self.connection_state == ConnectionState.CONNECTED and
                    self.ws and
                    not self.ws.closed
                )

                if is_ws_connected:
                    logger.info(f"【{log_account_id}】WebSocket连接正常，Token刷新异常可能是暂时的，跳过失败知")
                else:
                    logger.warning(f"【{log_account_id}】WebSocket未连接，发送Token刷新异常通知")
                    await self.send_token_refresh_notification(f"Token刷新异常: {str(e)}", "token_refresh_exception")
            else:
                logger.info(f"【{log_account_id}】已发滑块验证相关通知，跳过Token刷新异常通知")
            return None

    def _need_captcha_verification(self, res_json: dict) -> bool:
        try:
            if not isinstance(res_json, dict):
                return False

            current_account_id = self._canonical_account_id()
            account_log_label = current_account_id or "default"

            res_json_str = json.dumps(res_json, ensure_ascii=False, separators=(',', ':'))
            log_captcha_event(current_account_id, "检查滑块验证响应", None, f"res_json内容: {res_json_str}")

            ret_value = res_json.get('ret', [])

            captcha_keywords = [
                'FAIL_SYS_USER_VALIDATE',
                'RGV587_ERROR',
                '哎哟喂，被挤爆啦',
                '哎哟喂，被挤爆啦',
                '挤爆',
                '请稍后重试',
                'punish?x5secdata',
                'captcha',
                ]

            error_msg = str(ret_value[0]) if ret_value else ''

            for keyword in captcha_keywords:
                if keyword in error_msg:
                    logger.info(f"【{account_log_label}】检测到需要滑块验证的关键词: {keyword}")
                    return True

            data = res_json.get('data', {})
            if isinstance(data, dict) and 'url' in data:
                url = str(data.get('url', '') or '')
                if 'punish' in url or 'captcha' in url or 'validate' in url:
                    logger.info(f"【{account_log_label}】检测到验证URL: {url}")
                    return True

            return False

        except Exception as e:
            logger.error(f"【{self._canonical_account_id() or 'default'}】检查是否需要滑块验证时出错: {self._safe_str(e)}")
            return False

    def _run_slider_verification_with_managed_runtime_sync(
        self,
        *,
        slider,
        verification_url: str,
        purpose: str,
        release_reason: str,
        attach_failure_reason: str,
    ):
        canonical_account_id = self._canonical_account_id()
        if not canonical_account_id:
            raise RuntimeError("滑块验证缺少 canonical account_id，无法申请账号级浏览器 runtime")

        request_builder = getattr(slider, "build_managed_runtime_request", None)
        if not callable(request_builder):
            raise AttributeError(f"{type(slider).__name__} 缺少 build_managed_runtime_request")

        attach_managed_runtime = getattr(slider, "attach_managed_runtime", None)
        if not callable(attach_managed_runtime):
            raise AttributeError(f"{type(slider).__name__} 缺少 attach_managed_runtime")

        runtime_request = request_builder(
            account_id=canonical_account_id,
            purpose=purpose,
        )
        lease = account_browser_runtime_manager.acquire_runtime_sync(
            canonical_account_id,
            purpose,
            exclusive=True,
            runtime_request=runtime_request,
        )
        attach_succeeded = False
        current_release_reason = attach_failure_reason
        try:
            page, context = account_browser_runtime_manager.get_fresh_page_sync(lease)
            runtime = getattr(lease, "runtime", None)
            browser = getattr(runtime, "browser", None) or getattr(context, "browser", None)
            playwright = getattr(runtime, "playwright", None)
            attach_managed_runtime(
                lease=lease,
                runtime=runtime,
                browser=browser,
                context=context,
                page=page,
                playwright=playwright,
                browser_features=runtime_request.get("browser_features"),
                profile_id=runtime_request.get("profile_id"),
            )
            attach_succeeded = True
            current_release_reason = release_reason
            return slider.run(
                verification_url,
                require_managed_runtime=True,
            )
        finally:
            if not attach_succeeded:
                detach_managed_runtime = getattr(slider, "_detach_managed_runtime", None)
                if callable(detach_managed_runtime):
                    try:
                        detach_managed_runtime()
                    except Exception:
                        pass
            account_browser_runtime_manager.release_runtime_sync(
                lease,
                reason=current_release_reason,
            )

    async def _handle_captcha_verification(self, res_json: dict) -> str:
        try:
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error("【default】滑块验证缺少 canonical account_id，拒绝继续运行")
                return None
            logger.info(f"【{current_account_id}】开始处理滑块验证...")

            if self.is_manual_refresh_active(current_account_id, allow_handoff_recovery=True):
                logger.warning(f"【{current_account_id}】手动刷新进行中，取消自动滑块处理")
                log_captcha_event(
                    current_account_id,
                    "手动刷新进行中，取消自动滑块处理",
                    None,
                    "自动滑块处理已跳过"
                )
                return None

            verification_url = None

            data = res_json.get('data', {})
            if isinstance(data, dict) and 'url' in data:
                verification_url = data.get('url')

            if not verification_url:
                logger.info(f"【{current_account_id}】未找到验证URL，认为不需要滑块验证，返回正常")
                return None

            logger.info(f"【{current_account_id}】验证URL: {verification_url}")

            try:
                from utils.xianyu_slider_stealth import XianyuSliderStealth

                logger.info(f"【{current_account_id}】XianyuSliderStealth导入成功，使用滑块验证")

                account_info = db_manager.get_cookie_details(current_account_id) or {}
                logger.info(
                    f"【{current_account_id}】自动滑块验证准备启动："
                    f"headless=True, proxy_type={self.proxy_config.get('proxy_type', 'none')}"
                )

                slider_stealth = XianyuSliderStealth(
                    user_id=f"{current_account_id}",
                    enable_learning=True,
                    headless=True,
                    initial_cookies=self.cookies_str,
                    proxy=self.proxy_config,
                    use_account_persistent_profile=True,
                )
                slider_stealth.risk_trigger_scene = 'token_refresh'

                success, cookies = await slider_stealth._run_sync_method_on_fresh_thread(
                    self._run_slider_verification_with_managed_runtime_sync,
                    slider=slider_stealth,
                    verification_url=verification_url,
                    purpose="token_refresh_slider",
                    release_reason="token_refresh_slider_completed",
                    attach_failure_reason="token_refresh_slider_attach_failed",
                )

                if success and cookies:
                    logger.info(f"【{self.account_id}】滑块验证成功，获取到新的cookies")

                    current_cookies_dict = trans_cookies(self.cookies_str)
                    x5sec_cookies = {}

                    for cookie_name, cookie_value in cookies.items():
                        cookie_name_lower = cookie_name.lower()
                        if cookie_name_lower.startswith('x5') or 'x5sec' in cookie_name_lower:
                            x5sec_cookies[cookie_name] = cookie_value

                    logger.info(f"【{self.account_id}】找到{len(x5sec_cookies)}个x5相关cookies: {list(x5sec_cookies.keys())}")

                    merge_result = self.protected_merge_cookie_dicts(current_cookies_dict, cookies)
                    updated_cookies = merge_result['merged_cookies_dict']
                    updated_fields = merge_result['updated_fields']
                    changed_fields = merge_result['changed_fields']
                    new_fields = merge_result['new_fields']
                    removed_fields = merge_result['removed_fields']
                    preserved_fields = merge_result['preserved_fields']
                    preserved_protected_fields = merge_result['preserved_protected_fields']
                    would_remove_fields = merge_result['would_remove_fields']
                    missing_protected_fields = merge_result['missing_protected_fields']
                    missing_required_fields = merge_result['missing_required_fields']
                    incoming_missing_protected_fields = merge_result['incoming_missing_protected_fields']
                    account_switched = merge_result['account_switched']
                    cookies_str = "; ".join([f"{k}={v}" for k, v in updated_cookies.items()])
                    qr_login_grace = self.get_qr_login_grace(current_account_id)
                    merge_event_name = "slider_post_qr_protected_merge" if qr_login_grace else "captcha_protected_merge"
                    self._log_protected_merge_event(merge_event_name, merge_result)

                    self._log_cookie_merge_summary(
                        updated_cookies,
                        updated_fields,
                        changed_fields,
                        new_fields,
                        context="滑块验证成功后Cookie合并",
                        preserved_fields=preserved_fields,
                        preserved_protected_fields=preserved_protected_fields,
                        would_remove_fields=would_remove_fields,
                        removed_fields=removed_fields,
                        missing_protected_fields=missing_protected_fields,
                        missing_required_fields=missing_required_fields,
                        incoming_missing_protected_fields=incoming_missing_protected_fields,
                        account_switched=account_switched,
                    )

                    if missing_required_fields:
                        accept_business_ready_handoff = False
                        helper = getattr(slider_stealth, '_should_accept_business_ready_cookie_handoff', None)
                        if callable(helper):
                            try:
                                accept_business_ready_handoff = bool(
                                    helper(
                                        updated_cookies,
                                        missing_required_fields=missing_required_fields,
                                    )
                                )
                            except Exception as business_ready_err:
                                logger.warning(
                                    f"【{self.account_id}】滑块验证后评估 business-ready Cookie 失败: "
                                    f"{self._safe_str(business_ready_err)}"
                                )
                        if accept_business_ready_handoff:
                            logger.warning(
                                f"【{self.account_id}】滑块验证后的Cookie仅缺少 cna，"
                                "但浏览器业务预热已证明会话可用，继续写回数据库"
                            )
                        else:
                            logger.error(f"【{self.account_id}】滑块验证后的Cookie仍缺失核心字段，放弃写回数据库: {', '.join(missing_required_fields)}")
                            return None

                    try:
                        old_cookies_str = self.cookies_str
                        old_cookies_dict = self.cookies.copy()

                        self._set_runtime_cookie_state(
                            cookies_str=cookies_str,
                            cookies_dict=updated_cookies,
                            source="slider_success",
                        )

                        await self.update_config_cookies()
                        logger.info(f"【{current_account_id}】滑块验证成功后，cookies已自动更新到数据库")
                        self._mark_slider_success_recovery(cookies_str)
                        self._mark_pending_slider_success_notice("token_refresh")
                        XianyuLive.clear_password_login_failure_backoff(current_account_id)
                        logger.info(f"【{current_account_id}】滑块验证成功后，已清理密码登录失败回避状态")

                        x5sec_cookies_str = "; ".join([f"{k}={v}" for k, v in x5sec_cookies.items()]) if x5sec_cookies else "?"
                        log_captcha_event(current_account_id, "滑块验证成功并自动更新数据库", True,
                            f"原有{len(current_cookies_dict)}个cookie项, 浏览器快照{len(cookies)}个, 合并后{len(updated_cookies)}个, 变更字段{len(changed_fields)}个, 新增字段{len(new_fields)}个, 保护保留{len(preserved_protected_fields)}个, 实际移除{len(removed_fields)}个, x5 cookies: {x5sec_cookies_str}")

                    except Exception as update_e:
                        logger.error(f"【{current_account_id}】自动更新数据库cookies失败: {self._safe_str(update_e)}")

                        self._set_runtime_cookie_state(
                            cookies_str=old_cookies_str,
                            cookies_dict=old_cookies_dict,
                            source="slider_success_rollback",
                        )

                        x5sec_cookies_str = "; ".join([f"{k}={v}" for k, v in x5sec_cookies.items()]) if x5sec_cookies else "?"
                        log_captcha_event(current_account_id, "滑块验证成功但数据库更新失败", False,
                            f"更新异常: {self._safe_str(update_e)[:100]}, 变更字段{len(changed_fields)}个, 新增字段{len(new_fields)}个, 保护保留{len(preserved_protected_fields)}个, 获取到的x5 cookies: {x5sec_cookies_str}")

                        await self.send_token_refresh_notification(
                            f"滑块验证成功但数据库更新失败: {self._safe_str(update_e)}",
                            "captcha_success_db_update_failed"
                        )

                        return None

                    return cookies_str
                else:
                    logger.error(f"【{current_account_id}】滑块验证失败")
                    slider_error = getattr(slider_stealth, 'last_login_error', '') or "滑块验证失败，未获取到新Cookie"
                    self.last_token_refresh_status = "captcha_verification_failed"
                    self.last_token_refresh_error_message = slider_error

                    log_captcha_event(current_account_id, "滑块验证失败", False,
                        f"{slider_error}, 环境: {'Docker' if os.getenv('DOCKER_ENV') else '本地'}")

                    is_ws_connected = (
                        self.connection_state == ConnectionState.CONNECTED and
                        self.ws and
                        not self.ws.closed
                    )

                    if is_ws_connected:
                        logger.info(f"【{current_account_id}】WebSocket连接正常，滑块验证失败可能是暂时的，跳过通知")
                    else:
                        logger.warning(f"【{current_account_id}】WebSocket未连接，发滑块验证失败知")
                        await self.send_token_refresh_notification(
                            f"滑块验证失败，需要手动处理验证URL: {verification_url}",
                            "captcha_verification_failed"
                        )
                    return None

            except ImportError as import_e:
                logger.error(f"【{current_account_id}】XianyuSliderStealth导入失败: {import_e}")
                logger.error(f"【{current_account_id}】请安装 CloakBrowser 运行时: python -m cloakbrowser install")

                log_captcha_event(current_account_id, "XianyuSliderStealth导入失败", False,
                    f"Playwright未安装, 错误: {import_e}")

                await self.send_token_refresh_notification(
                    f"滑块验证功能不可用，请安装Playwright。验证URL: {verification_url}",
                    "captcha_dependency_missing"
                )
                return None

            except Exception as stealth_e:
                logger.error(f"【{current_account_id}】滑块验证异常: {self._safe_str(stealth_e)}")
                import traceback
                logger.error(f"【{current_account_id}】滑块验证异常堆栈:\n{traceback.format_exc()}")
                self.last_token_refresh_status = "captcha_execution_error"
                self.last_token_refresh_error_message = self._safe_str(stealth_e)

                log_captcha_event(current_account_id, "滑块验证异常", False,
                    f"执行异常, 错误: {self._safe_str(stealth_e)[:100]}")

                is_ws_connected = (
                    self.connection_state == ConnectionState.CONNECTED and
                    self.ws and
                    not self.ws.closed
                )

                if is_ws_connected:
                    logger.info(f"【{current_account_id}】WebSocket连接正常，滑块验证执行异常可能是暂时的，跳过通知")
                else:
                    logger.warning(f"【{current_account_id}】WebSocket未连接，发滑块验证执行异常知")
                    await self.send_token_refresh_notification(
                        f"滑块验证执行异常，需要手动处理验证URL: {verification_url}",
                        "captcha_execution_error"
                    )
                return None



        except Exception as e:
            logger.error(f"【{current_account_id}】处理滑块验证时出错: {self._safe_str(e)}")
            return None

    async def _update_cookies_and_restart(self, new_cookies_str: str):
        log_account_id = self._canonical_account_id() or "default"
        try:
            logger.info(f"【{log_account_id}】开始更新cookies并重启任务...")

            if not new_cookies_str or not new_cookies_str.strip():
                logger.error(f"【{log_account_id}】新cookies为空，无法更新")
                return False

            try:
                new_cookies_dict = trans_cookies(new_cookies_str)
                if not new_cookies_dict:
                    logger.error(f"【{log_account_id}】新cookies解析失败，无法更新")
                    return False
                logger.info(f"【{log_account_id}】新cookies解析成功，包含 {len(new_cookies_dict)} 个字段")
            except Exception as parse_e:
                logger.error(f"【{log_account_id}】新cookies解析异常: {self._safe_str(parse_e)}")
                return False

            try:
                merge_result = self.protected_merge_cookie_dicts(trans_cookies(self.cookies_str), new_cookies_dict)
                merged_cookies_dict = merge_result['merged_cookies_dict']
                updated_fields = merge_result['updated_fields']
                changed_fields = merge_result['changed_fields']
                new_fields = merge_result['new_fields']
                self._log_protected_merge_event("password_refresh_protected_merge", merge_result)

                self._log_cookie_merge_summary(
                    merged_cookies_dict,
                    updated_fields,
                    changed_fields,
                    new_fields,
                    context="密码登录刷新Cookie",
                    preserved_fields=merge_result['preserved_fields'],
                    preserved_protected_fields=merge_result['preserved_protected_fields'],
                    would_remove_fields=merge_result['would_remove_fields'],
                    removed_fields=merge_result['removed_fields'],
                    missing_protected_fields=merge_result['missing_protected_fields'],
                    missing_required_fields=merge_result['missing_required_fields'],
                    incoming_missing_protected_fields=merge_result['incoming_missing_protected_fields'],
                    account_switched=merge_result['account_switched'],
                )

                if merge_result['missing_required_fields']:
                    logger.error(
                        f"【{log_account_id}】密码登录刷新后的Cookie仍缺失核心字段，放弃写回并重启: {', '.join(merge_result['missing_required_fields'])}"
                    )
                    return False

                new_cookies_str = '; '.join([f"{k}={v}" for k, v in merged_cookies_dict.items()])
                new_cookies_dict = merged_cookies_dict

            except Exception as merge_e:
                logger.error(f"【{log_account_id}】cookies合并异常: {self._safe_str(merge_e)}")
                logger.warning(f"【{log_account_id}】将使用原始新cookies（不合并）")

            old_cookies_str = self.cookies_str
            old_cookies_dict = self.cookies.copy()

            try:
                self._set_runtime_cookie_state(
                    cookies_str=new_cookies_str,
                    cookies_dict=new_cookies_dict,
                    source="password_login_refresh",
                )

                await self.update_config_cookies()
                logger.info(f"【{log_account_id}】数据库cookies更新成功")

                logger.info(f"【{log_account_id}】cookies更新成功，准备重启任务...")

                logger.info(f"【{log_account_id}】过CookieManager触发重启...")
                await self._restart_instance()

                logger.info(f"【{log_account_id}】重启请求已触发，等待任务被取消...")
                return True

            except Exception as update_e:
                logger.error(f"【{log_account_id}】更新cookies过程中出错，尝试回滚: {self._safe_str(update_e)}")

                try:
                    self._set_runtime_cookie_state(
                        cookies_str=old_cookies_str,
                        cookies_dict=old_cookies_dict,
                        source="password_login_refresh_rollback",
                    )
                    await self.update_config_cookies()
                    logger.info(f"【{log_account_id}】cookies已回滚到原始状态")
                except Exception as rollback_e:
                    logger.error(f"【{log_account_id}】cookies回滚失败: {self._safe_str(rollback_e)}")

                return False

        except Exception as e:
            logger.error(f"【{log_account_id}】更新cookies并重启任务时出错: {self._safe_str(e)}")
            return False

    async def update_config_cookies(self):
        try:
            from db_manager import db_manager

            current_account_id = self._canonical_account_id()
            log_account_id = current_account_id or "default"
            if current_account_id:
                try:
                    current_user_id = None
                    if hasattr(self, 'user_id') and self.user_id:
                        current_user_id = self.user_id

                    success = db_manager.update_cookie_account_info(
                        current_account_id,
                        cookie_value=self.cookies_str,
                        user_id=current_user_id
                    )
                    if not success:
                        logger.warning(f"更新Cookie到数据库失败: {current_account_id}，但不使用save_cookie避免覆盖账号密码")
                    else:
                        logger.warning(f"已更新Cookie到数据库: {current_account_id}")
                except Exception as e:
                    logger.error(f"【{log_account_id}】更新数据库Cookie失败: {self._safe_str(e)}")
                    await self.send_token_refresh_notification(f"数据库Cookie更新失败: {str(e)}", "db_update_failed")
            else:
                logger.warning("account_id不存在，无法更新数据")
                await self.send_token_refresh_notification("account_id不存在，无法更新数据", "account_id_missing")

        except Exception as e:
            logger.error(f"【{self._canonical_account_id() or 'default'}】更新Cookie失败: {self._safe_str(e)}")
            await self.send_token_refresh_notification(f"Cookie更新失败: {str(e)}", "cookie_update_failed")

    async def _try_password_login_refresh(
        self,
        trigger_reason: str = "令牌/Session过期",
        risk_session_id: Optional[str] = None,
        trigger_scene: Optional[str] = None,
        ignore_slider_failed_backoff: bool = False,
    ):
        trigger_scene = trigger_scene or self._normalize_risk_trigger_scene(trigger_reason, default='auto_cookie_refresh')
        risk_session_id = risk_session_id or self._new_risk_session_id('cookie')
        risk_log_started_at = time.time()
        runtime_account_id = self._canonical_account_id()
        log_account_id = runtime_account_id or "default"
        logger.warning(f"【{log_account_id}】检测到{trigger_reason}，准备刷新Cookie并重启实例...")
        if not runtime_account_id:
            logger.error("【default】密码登录刷新缺少 canonical account_id，无法继续申请账号级浏览器 runtime")
            return False
        base_event_meta = {'account_id': runtime_account_id, 'trigger_reason': trigger_reason}

        refresh_risk_log_id = None
        try:
            stale_count = db_manager.mark_stale_risk_control_logs_failed(
                timeout_minutes=15,
                account_id=runtime_account_id,
            )
            if stale_count > 0:
                logger.warning(f"【{log_account_id}】检测到{stale_count}条超时processing风控日志，已自动标记failed")
            refresh_risk_log_id = self._create_risk_log(
                event_type='cookie_refresh',
                session_id=risk_session_id,
                trigger_scene=trigger_scene,
                result_code='cookie_refresh_started',
                event_description=f"{trigger_reason}触发Cookie刷新",
                processing_status='processing',
                event_meta=self._build_risk_event_meta(trigger_scene=trigger_scene, extra=base_event_meta),
            )
        except Exception as log_e:
            logger.error(f"【{log_account_id}】记录风控日志失败: {log_e}")

        if self.is_manual_refresh_active(runtime_account_id, allow_handoff_recovery=True):
            logger.warning(f"【{log_account_id}】手动刷新进行中，跳过自动密码登录刷新")
            if refresh_risk_log_id:
                self._update_risk_log(
                    refresh_risk_log_id,
                    session_id=risk_session_id,
                    trigger_scene=trigger_scene,
                    result_code='manual_refresh_active',
                    processing_status='failed',
                    error_message='手动刷新进行中，自动密码登录刷新已跳',
                    duration_ms=max(0, int((time.time() - risk_log_started_at) * 1000)),
                    event_meta=self._build_risk_event_meta(trigger_scene=trigger_scene, extra=base_event_meta),
                )
            return False

        recovery_lock_owner = f"{runtime_account_id}:{trigger_scene or 'auto_cookie_refresh'}:{int(time.time() * 1000)}"
        recovery_lock_acquired = False

        current_time = time.time()
        failure_backoff = XianyuLive.get_password_login_failure_backoff(runtime_account_id)
        if failure_backoff:
            remaining_time = failure_backoff.get('until', 0) - current_time
            if remaining_time > 0:
                backoff_reason = failure_backoff.get('reason', 'unknown')
                if backoff_reason == 'slider_failed' and (
                    ignore_slider_failed_backoff or self.consume_manual_refresh_slider_failed_bypass(runtime_account_id)
                ):
                    logger.warning(
                        f"【{log_account_id}】检测到最近刚通过滑块或处于刷新交接恢复窗口，忽略一次旧的 slider_failed 退避并继续尝试密码登录刷新"
                    )
                    XianyuLive.clear_password_login_failure_backoff(runtime_account_id)
                    failure_backoff = None
                else:
                    logger.warning(
                        f"【{log_account_id}】密码登录失败退避中（原因: {backoff_reason}），还需等待 {remaining_time:.1f} 秒"
                    )
                    if refresh_risk_log_id:
                        self._update_risk_log(
                            refresh_risk_log_id,
                            session_id=risk_session_id,
                            trigger_scene=trigger_scene,
                            result_code='password_login_backoff',
                            processing_status='failed',
                            error_message=f"密码登录失败退避中，剩余{remaining_time:.1f}秒",
                            duration_ms=max(0, int((time.time() - risk_log_started_at) * 1000)),
                            event_meta=self._build_risk_event_meta(
                                trigger_scene=trigger_scene,
                                extra={**base_event_meta, 'backoff_reason': backoff_reason, 'backoff_seconds': failure_backoff.get('seconds')},
                            ),
                        )
                    return False

        last_password_login = XianyuLive._last_password_login_time.get(runtime_account_id, 0)
        time_since_last_login = current_time - last_password_login

        if last_password_login > 0 and time_since_last_login < XianyuLive._password_login_cooldown:
            remaining_time = XianyuLive._password_login_cooldown - time_since_last_login
            logger.warning(f"【{log_account_id}】距离上次密码登录仅 {time_since_last_login:.1f} 秒，仍在冷却期内（还需等待 {remaining_time:.1f} 秒），跳过密码登录")
            logger.warning(f"【{log_account_id}】提示：如果新Cookie仍然无效，请棢查账号状态或手动更新Cookie")
            if refresh_risk_log_id:
                self._update_risk_log(
                    refresh_risk_log_id,
                    session_id=risk_session_id,
                    trigger_scene=trigger_scene,
                    result_code='password_login_cooldown',
                    processing_status='failed',
                    error_message=f"密码登录冷却期内，剩余{remaining_time:.1f}秒",
                    duration_ms=max(0, int((time.time() - risk_log_started_at) * 1000)),
                    event_meta=self._build_risk_event_meta(trigger_scene=trigger_scene, extra=base_event_meta),
                )
            return False

        recovery_lock_acquired, existing_lock = XianyuLive.acquire_auth_recovery_lock(
            runtime_account_id,
            recovery_lock_owner,
        )
        if not recovery_lock_acquired:
            existing_owner = (existing_lock or {}).get('owner', 'unknown')
            logger.warning(f"【{log_account_id}】认证恢复流程已在执行中，跳过本次重复触发: owner={existing_owner}")
            if refresh_risk_log_id:
                self._update_risk_log(
                    refresh_risk_log_id,
                    session_id=risk_session_id,
                    trigger_scene=trigger_scene,
                    result_code='auth_recovery_in_progress',
                    processing_status='failed',
                    error_message='已有认证恢复流程执行',
                    duration_ms=max(0, int((time.time() - risk_log_started_at) * 1000)),
                    event_meta=self._build_risk_event_meta(
                        trigger_scene=trigger_scene,
                        extra={**base_event_meta, 'active_owner': existing_owner},
                    ),
                )
            return False

        log_captcha_event(runtime_account_id, f"{trigger_reason}触发Cookie刷新和实例重启", None,
            f"检测到{trigger_reason}，准备刷新Cookie并重启实例")

        try:
            account_info = db_manager.get_cookie_details(runtime_account_id)

            if not account_info:
                logger.error(f"【{log_account_id}】无法获取账号信息")
                self.last_token_refresh_error_message = "无法获取账号信息"
                if refresh_risk_log_id:
                    self._update_risk_log(
                        refresh_risk_log_id,
                        session_id=risk_session_id,
                        trigger_scene=trigger_scene,
                        result_code='account_info_missing',
                        processing_status='failed',
                        error_message='无法获取账号信息',
                        duration_ms=max(0, int((time.time() - risk_log_started_at) * 1000)),
                        event_meta=self._build_risk_event_meta(trigger_scene=trigger_scene, extra=base_event_meta),
                    )
                return False

            db_cookie_value = account_info.get('cookie_value', '')
            if db_cookie_value and db_cookie_value != self.cookies_str:
                logger.info(f"【{log_account_id}】检测到数据库中的cookie已更新，重新加载cookie")
                self._set_runtime_cookie_state(cookies_str=db_cookie_value, source="db_cookie_reload_before_password_login")
                logger.info(f"【{log_account_id}】Cookie已从数据库重新加载，跳过密码登录刷新")
                if refresh_risk_log_id:
                    self._update_risk_log(
                        refresh_risk_log_id,
                        session_id=risk_session_id,
                        trigger_scene=trigger_scene,
                        result_code='cookie_already_updated',
                        processing_status='success',
                        processing_result='棢测到数据库Cookie已更新，自动刷新流程跳过',
                        duration_ms=max(0, int((time.time() - risk_log_started_at) * 1000)),
                        event_meta=self._build_risk_event_meta(trigger_scene=trigger_scene, extra=base_event_meta),
                    )
                return True

            username = account_info.get('username', '')
            password = account_info.get('password', '')
            if not username or not password:
                logger.warning(f"【{log_account_id}】未配置用户名或密码，跳过密码登录刷新")
                self.last_token_refresh_status = "no_credentials"
                self.last_token_refresh_error_message = "未配置用户名或密码，无法自动刷新Cookie"
                await self.send_token_refresh_notification(
                    f"检测到{trigger_reason}，但未配置用户名或密码，无法自动刷新Cookie",
                    "no_credentials"
                )
                if refresh_risk_log_id:
                    self._update_risk_log(
                        refresh_risk_log_id,
                        session_id=risk_session_id,
                        trigger_scene=trigger_scene,
                        result_code='missing_credentials',
                        processing_status='failed',
                        error_message='未配置用户名或密码，无法自动刷新Cookie',
                        duration_ms=max(0, int((time.time() - risk_log_started_at) * 1000)),
                        event_meta=self._build_risk_event_meta(trigger_scene=trigger_scene, extra=base_event_meta),
                    )
                return False

            logger.info(f"【{log_account_id}】开始使用无头浏览器进行密码登录刷新Cookie...")
            logger.info(f"【{log_account_id}】使用账号 {username}")

            async def notification_callback_wrapper(message: str, screenshot_path: str = None, verification_url: str = None):
                await self.send_token_refresh_notification(
                    error_message=message,
                    notification_type="token_refresh",
                    chat_id=None,
                    attachment_path=screenshot_path,
                    verification_url=verification_url
                )

            import asyncio
            from utils.xianyu_slider_stealth import XianyuSliderStealth
            (
                reuse_account_persistent_profile,
                reuse_account_persistent_profile_reason,
            ) = self._should_prefer_account_persistent_profile_for_browser_recovery()
            resolved_account_id = runtime_account_id
            slider = XianyuSliderStealth(
                user_id=resolved_account_id,
                enable_learning=True,
                headless=True,
                initial_cookies=self.cookies_str,
                proxy=self.proxy_config,
                use_account_persistent_profile=reuse_account_persistent_profile,
                account_persistent_profile_dir=self._resolve_account_browser_profile_dir(
                    profile_key=resolved_account_id,
                ),
            )
            slider.risk_session_id = risk_session_id
            slider.risk_trigger_scene = trigger_scene
            if reuse_account_persistent_profile:
                logger.warning(
                    f"【{log_account_id}】{reuse_account_persistent_profile_reason}，"
                    "优先复用账号持久化画像，禁用干净上下文"
                )
                try:
                    invalidated = await slider._run_sync_method_on_fresh_thread(
                        account_browser_runtime_manager.invalidate_runtime_sync,
                        resolved_account_id,
                        reason="password_login_refresh_prepare",
                    )
                    if invalidated:
                        logger.info(f"【{log_account_id}】密码登录刷新前已主动失效旧的账号级浏览器 runtime")
                        await asyncio.sleep(0.8)
                except Exception as invalidate_error:
                    logger.warning(
                        f"【{log_account_id}】密码登录刷新前失效账号级浏览器 runtime 失败，继续尝试启动新 runtime: "
                        f"{self._safe_str(invalidate_error)}"
                    )
            result = await slider._run_sync_method_on_fresh_thread(
                self._run_password_login_with_managed_runtime,
                slider=slider,
                resolved_account_id=resolved_account_id,
                account=username,
                password=password,
                notification_callback=notification_callback_wrapper,
                force_clean_context=not reuse_account_persistent_profile,
            )

            if result:
                logger.info(f"【{log_account_id}】密码登录成功，获取到Cookie")
                logger.info(f"【{log_account_id}】Cookie内容: {result}")
                XianyuLive.clear_password_login_failure_backoff(runtime_account_id)

                merge_result = self.protected_merge_cookie_dicts(self.cookies, result)
                self._log_protected_merge_event("password_login_protected_merge", merge_result)
                self._log_cookie_merge_summary(
                    merge_result['merged_cookies_dict'],
                    merge_result['updated_fields'],
                    merge_result['changed_fields'],
                    merge_result['new_fields'],
                    context="密码登录Cookie合并",
                    preserved_fields=merge_result['preserved_fields'],
                    preserved_protected_fields=merge_result['preserved_protected_fields'],
                    would_remove_fields=merge_result['would_remove_fields'],
                    removed_fields=merge_result['removed_fields'],
                    missing_protected_fields=merge_result['missing_protected_fields'],
                    missing_required_fields=merge_result['missing_required_fields'],
                    incoming_missing_protected_fields=merge_result['incoming_missing_protected_fields'],
                    account_switched=merge_result['account_switched'],
                )

                if merge_result['missing_required_fields']:
                    logger.error(
                        f"【{log_account_id}】密码登录后的Cookie合并结果仍缺失核心字段，放弃继续交接: "
                        f"{', '.join(merge_result['missing_required_fields'])}"
                    )
                    self.last_token_refresh_error_message = (
                        "密码登录后的Cookie合并结果缺失核心字段: "
                        + ', '.join(merge_result['missing_required_fields'])
                    )
                    if refresh_risk_log_id:
                        self._update_risk_log(
                            refresh_risk_log_id,
                            session_id=risk_session_id,
                            trigger_scene=trigger_scene,
                            result_code='password_login_cookie_incomplete',
                            processing_status='failed',
                            error_message=self.last_token_refresh_error_message[:200],
                            duration_ms=max(0, int((time.time() - risk_log_started_at) * 1000)),
                            event_meta=self._build_risk_event_meta(trigger_scene=trigger_scene, extra=base_event_meta),
                        )
                    return False

                result = merge_result['merged_cookies_dict']

                logger.info(f"【{log_account_id}】========== 密码登录Cookie字段详情 ==========")
                logger.info(f"【{log_account_id}】Cookie字段数: {len(result)}")
                logger.info(f"【{log_account_id}】Cookie字段列表:")
                for i, (key, value) in enumerate(result.items(), 1):
                    if len(str(value)) > 50:
                        logger.info(f"【{log_account_id}】  {i:2d}. {key}: {str(value)[:30]}...{str(value)[-20:]} (长度: {len(str(value))})")
                    else:
                        logger.info(f"【{log_account_id}】  {i:2d}. {key}: {value}")

                important_keys = list(REQUIRED_SESSION_COOKIE_FIELDS) + list(OBSERVED_SESSION_COOKIE_FIELDS)
                logger.info(f"【{log_account_id}】关键字段检查:")
                for key in important_keys:
                    if key in result:
                        val = result[key]
                        logger.info(f"【{log_account_id}】  ✅ {key}: {'存在' if val else '为空'} (长度: {len(str(val)) if val else 0})")
                    else:
                        logger.info(f"【{log_account_id}】  ❌ {key}: 缺失")
                logger.info(f"【{log_account_id}】==========================================")

                new_cookies_str = '; '.join([f"{k}={v}" for k, v in result.items()])
                logger.info(f"【{log_account_id}】Cookie字符串摘要: {self._summarize_cookie_string(new_cookies_str)}")

                try:
                    preflight_xianyu = XianyuLive(
                        cookies_str=new_cookies_str,
                        account_id=runtime_account_id,
                        user_id=self.user_id,
                        register_instance=False,
                    )
                    await preflight_xianyu.preflight_token_after_password_login()
                    if preflight_xianyu.cookies_str and preflight_xianyu.cookies_str != new_cookies_str:
                        new_cookies_str = preflight_xianyu.cookies_str
                        result = trans_cookies(new_cookies_str)
                        logger.info(f"【{log_account_id}】密码登录后的Token预检通过，将使用预检确认后的Cookie继续交接")
                        logger.info(f"【{log_account_id}】预检后Cookie字符串摘要: {self._summarize_cookie_string(new_cookies_str)}")
                except Exception as preflight_err:
                    preflight_error = self._safe_str(preflight_err)
                    logger.error(f"【{log_account_id}】密码登录成功，但Token预检失败: {preflight_error}")
                    self.last_token_refresh_error_message = f"密码登录后的Token预检失败: {preflight_error}"
                    XianyuLive.clear_auth_prewarmed_token(runtime_account_id)
                    if refresh_risk_log_id:
                        self._update_risk_log(
                            refresh_risk_log_id,
                            session_id=risk_session_id,
                            trigger_scene=trigger_scene,
                            result_code='password_login_preflight_failed',
                            processing_status='failed',
                            error_message=self.last_token_refresh_error_message[:200],
                            duration_ms=max(0, int((time.time() - risk_log_started_at) * 1000)),
                            event_meta=self._build_risk_event_meta(trigger_scene=trigger_scene, extra=base_event_meta),
                        )
                    return False

                logger.warning(f"【{log_account_id}】已记录密码登录时间，冷却期 {XianyuLive._password_login_cooldown} 秒")

                try:
                    await self.send_token_refresh_notification(
                        f"账号密码登录成功，Cookie已获取，准备更新并重启",
                        "cookie_refresh_success"
                    )
                except Exception as notify_e:
                    logger.warning(f"【{log_account_id}】发送通知失败: {self._safe_str(notify_e)}")

                update_success = await self._update_cookies_and_restart(new_cookies_str)

                if update_success:
                    logger.info(f"【{log_account_id}】Cookie更新并重启任务成功")
                    if refresh_risk_log_id:
                        self._update_risk_log(
                            refresh_risk_log_id,
                            session_id=risk_session_id,
                            trigger_scene=trigger_scene,
                            result_code='cookie_refresh_success',
                            processing_status='success',
                            processing_result='密码登录刷新Cookie成功，实例已重启',
                            duration_ms=max(0, int((time.time() - risk_log_started_at) * 1000)),
                            event_meta=self._build_risk_event_meta(trigger_scene=trigger_scene, extra=base_event_meta),
                        )
                    return True
                else:
                    logger.error(f"【{log_account_id}】Cookie更新失败")
                    if refresh_risk_log_id:
                        self._update_risk_log(
                            refresh_risk_log_id,
                            session_id=risk_session_id,
                            trigger_scene=trigger_scene,
                            result_code='cookie_save_failed',
                            processing_status='failed',
                            error_message='Cookie获取成功但更新到数据库失',
                            duration_ms=max(0, int((time.time() - risk_log_started_at) * 1000)),
                            event_meta=self._build_risk_event_meta(trigger_scene=trigger_scene, extra=base_event_meta),
                        )
                    return False

            else:
                login_error = getattr(slider, 'last_login_error', '') or "密码登录失败，未获取到Cookie"
                self.last_token_refresh_error_message = login_error
                backoff_reason, backoff_seconds = XianyuLive.classify_password_login_failure(login_error)
                XianyuLive.set_password_login_failure_backoff(runtime_account_id, backoff_reason, backoff_seconds)
                logger.warning(f"【{log_account_id}】密码登录失败，未获取到Cookie: {login_error}")
                logger.warning(f"【{log_account_id}】已进入失败退避期: {backoff_reason}, {backoff_seconds}秒")
                if refresh_risk_log_id:
                    self._update_risk_log(
                        refresh_risk_log_id,
                        session_id=risk_session_id,
                        trigger_scene=trigger_scene,
                        result_code=f'password_login_{backoff_reason}',
                        processing_status='failed',
                        error_message=login_error[:200],
                        duration_ms=max(0, int((time.time() - risk_log_started_at) * 1000)),
                        event_meta=self._build_risk_event_meta(
                            trigger_scene=trigger_scene,
                            extra={**base_event_meta, 'backoff_reason': backoff_reason, 'backoff_seconds': backoff_seconds},
                        ),
                    )
                return False

        except Exception as refresh_e:
            self.last_token_refresh_error_message = self._safe_str(refresh_e)
            backoff_reason, backoff_seconds = XianyuLive.classify_password_login_failure(str(refresh_e))
            XianyuLive.set_password_login_failure_backoff(runtime_account_id, backoff_reason, backoff_seconds)
            logger.error(f"【{log_account_id}】Cookie刷新或实例重启失败: {self._safe_str(refresh_e)}")
            import traceback
            logger.error(f"【{log_account_id}】详细堆栈:\n{traceback.format_exc()}")
            if refresh_risk_log_id:
                self._update_risk_log(
                    refresh_risk_log_id,
                    session_id=risk_session_id,
                    trigger_scene=trigger_scene,
                    result_code='cookie_refresh_exception',
                    processing_status='failed',
                    error_message=str(refresh_e)[:200],
                    duration_ms=max(0, int((time.time() - risk_log_started_at) * 1000)),
                    event_meta=self._build_risk_event_meta(trigger_scene=trigger_scene, extra=base_event_meta),
                )
        finally:
            if recovery_lock_acquired:
                XianyuLive.release_auth_recovery_lock(runtime_account_id, recovery_lock_owner)

    def _run_password_login_with_managed_runtime(
        self,
        *,
        slider,
        resolved_account_id: str,
        account: str,
        password: str,
        notification_callback,
        force_clean_context: bool,
    ):
        canonical_account_id = self._canonical_account_id()
        resolved_account_id = self._normalize_account_scope(resolved_account_id)
        if not canonical_account_id:
            raise RuntimeError("密码登录刷新缺少 canonical account_id，无法申请账号级浏览器 runtime")
        if resolved_account_id and resolved_account_id != canonical_account_id:
            raise RuntimeError(
                f"密码登录刷新拒绝跨账号 runtime 请求: account_id={resolved_account_id}"
            )
        resolved_account_id = resolved_account_id or canonical_account_id

        request_builder = getattr(slider, "build_managed_runtime_request", None)
        if not callable(request_builder):
            raise AttributeError(f"{type(slider).__name__} 缺少 build_managed_runtime_request")

        attach_managed_runtime = getattr(slider, "attach_managed_runtime", None)
        if not callable(attach_managed_runtime):
            raise AttributeError(f"{type(slider).__name__} 缺少 attach_managed_runtime")

        runtime_request = request_builder(
            account_id=resolved_account_id,
            purpose="password_login",
        )
        lease = account_browser_runtime_manager.acquire_runtime_sync(
            resolved_account_id,
            "password_login",
            exclusive=True,
            runtime_request=runtime_request,
        )
        attach_succeeded = False
        release_reason = "password_login_refresh_attach_failed"
        try:
            page, context = account_browser_runtime_manager.get_fresh_page_sync(lease)
            runtime = getattr(lease, "runtime", None)
            browser = getattr(runtime, "browser", None) or getattr(context, "browser", None)
            playwright = getattr(runtime, "playwright", None)
            attach_managed_runtime(
                lease=lease,
                runtime=runtime,
                browser=browser,
                context=context,
                page=page,
                playwright=playwright,
                browser_features=runtime_request.get("browser_features"),
                profile_id=runtime_request.get("profile_id"),
            )
            attach_succeeded = True
            release_reason = "password_login_refresh_completed"
            return slider.login_with_password_browser(
                account=account,
                password=password,
                notification_callback=notification_callback,
                force_clean_context=force_clean_context,
                require_managed_runtime=True,
            )
        finally:
            if not attach_succeeded:
                detach_managed_runtime = getattr(slider, "_detach_managed_runtime", None)
                if callable(detach_managed_runtime):
                    try:
                        detach_managed_runtime()
                    except Exception:
                        pass
            account_browser_runtime_manager.release_runtime_sync(
                lease,
                reason=release_reason,
            )

    async def _verify_cookie_validity(self) -> dict:
        logger.info(f"【{self.account_id}】开始验证Cookie有效性（使用真实API调用）...")

        # NOTE: This method is used as a "best-effort" cookie health probe.
        # Keep imports local to avoid module-level side effects at startup.
        import tempfile
        from secure_confirm_decrypted import SecureConfirm

        result = {
            'valid': True,
            'confirm_api': None,
            'web_session_api': None,
            'image_api': None,
            'details': [],
            'inconclusive': False,
            'relogin_recommended': True,
        }

        try:
            logger.info(f"【{self.account_id}】测试确认发货API（使用测试数据实际调用）...")

            if not self.session:
                connector = aiohttp.TCPConnector(limit=100, limit_per_host=30)
                timeout = aiohttp.ClientTimeout(total=30)
                self.session = aiohttp.ClientSession(connector=connector, timeout=timeout)

            confirm_tester = SecureConfirm(
                session=self.session,
                cookies_str=self.cookies_str,
                account_id=self.account_id,
                main_instance=self,
            )

            test_order_id = "999999999999999999"
            response = await confirm_tester.auto_confirm(test_order_id, retry_count=3)

            if response and isinstance(response, dict):
                error_msg = str(response.get('error', ''))
                success = response.get('success', False)

                if 'Session过期' in error_msg or 'SESSION_EXPIRED' in error_msg:
                    logger.warning(f"【{self.account_id}】❌ 确认发货API验证失败: Session过期")
                    result['confirm_api'] = False
                    result['valid'] = False
                    result['details'].append('确认发货API: Session过期')
                elif '令牌过期' in error_msg or 'TOKEN_EXPIRED' in error_msg:
                    logger.warning(f"【{self.account_id}】❌ 确认发货API验证失败: 令牌过期")
                    result['confirm_api'] = False
                    result['valid'] = False
                    result['details'].append('确认发货API: 令牌过期')
                elif success:
                    logger.info(f"【{self.account_id}】✅ 确认发货API验证通过: API调用成功")
                    result['confirm_api'] = True
                    result['details'].append('确认发货API: 通过验证')
                elif error_msg and len(error_msg) > 0:
                    logger.info(f"【{self.account_id}】✅ 确认发货API验证通过: Cookie有效（返回业务错误: {error_msg[:50]}）")
                    result['confirm_api'] = True
                    result['details'].append(f'确认发货API: Cookie有效（返回业务错误: {error_msg[:50]}）')
                else:
                    logger.warning(f"【{self.account_id}】⚠️ 确认发货API验证警告: 响应不明确")
                    result['confirm_api'] = None
                    result['inconclusive'] = True
                    if result['valid']:
                        result['relogin_recommended'] = False
                    result['details'].append('确认发货API: 响应不明确')
            else:
                logger.warning(f"【{self.account_id}】⚠️ 确认发货API验证警告: 无响应")
                result['confirm_api'] = None
                result['inconclusive'] = True
                if result['valid']:
                    result['relogin_recommended'] = False
                result['details'].append('确认发货API: 无响应')

        except Exception as e:
            error_str = self._safe_str(e)
            if 'Session过期' in error_str or 'SESSION_EXPIRED' in error_str:
                logger.warning(f"【{self.account_id}】❌ 确认发货API验证失败: Session过期")
                result['confirm_api'] = False
                result['valid'] = False
                result['details'].append('确认发货API: Session过期')
            else:
                logger.error(f"【{self.account_id}】确认发货API验证异常: {error_str}")
                result['confirm_api'] = None
                result['inconclusive'] = True
                if result['valid']:
                    result['relogin_recommended'] = False
                result['details'].append(f'确认发货API: 调用异常(可能非Cookie问题) - {error_str[:50]}')

        try:
            logger.info(f"【{self.account_id}】测试网页登录态（访问 IM 页面）...")

            if not self.session:
                connector = aiohttp.TCPConnector(limit=100, limit_per_host=30)
                timeout = aiohttp.ClientTimeout(total=30)
                self.session = aiohttp.ClientSession(connector=connector, timeout=timeout)

            async with self.session.get(
                'https://www.goofish.com/im',
                headers={
                    'cookie': self.cookies_str,
                    'Referer': 'https://www.goofish.com/',
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                },
                allow_redirects=True
            ) as response:
                final_url = str(response.url)
                page_text = await response.text()

                redirected_to_login = (
                    'passport.goofish.com' in final_url or
                    'mini_login' in final_url or
                    ('mini_login.htm' in page_text and 'alibaba-login-box' in page_text)
                )

                if redirected_to_login or response.status in (401, 403):
                    logger.warning(f"【{self.account_id}】❌ 网页登录态验证失败: 已进入登录/验证页 ({final_url})")
                    result['web_session_api'] = False
                    result['valid'] = False
                    result['details'].append("网页登录态: 已重定向到登录/验证页")
                elif response.status >= 500:
                    logger.warning(f"【{self.account_id}】⚠️ 网页登录态验证遇到服务端异常: HTTP {response.status}")
                    result['web_session_api'] = None
                    result['inconclusive'] = True
                    if result['valid']:
                        result['relogin_recommended'] = False
                    result['details'].append(f"网页登录态: 服务端异常，结果不确定 (HTTP {response.status})")
                elif response.status == 200:
                    logger.info(f"【{self.account_id}】✅ 网页登录态验证过: {final_url}")
                    result['web_session_api'] = True
                    result['details'].append("网页登录态: 通过验证")
                else:
                    logger.warning(f"【{self.account_id}】⚠️ 网页登录态验证结果不明确: HTTP {response.status}, URL={final_url}")
                    result['web_session_api'] = None
                    result['inconclusive'] = True
                    if result['valid']:
                        result['relogin_recommended'] = False
                    result['details'].append(f"网页登录态: 结果不明确 (HTTP {response.status})")

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            error_str = self._safe_str(e)
            logger.warning(f"【{self.account_id}】⚠️ 网页登录态验证网络异常: {error_str}")
            result['web_session_api'] = None
            result['inconclusive'] = True
            if result['valid']:
                result['relogin_recommended'] = False
            result['details'].append(f"网页登录态: 网络异常，结果不确定 ({error_str[:50]})")
        except Exception as e:
            error_str = self._safe_str(e)
            logger.error(f"【{self.account_id}】网页登录态验证异常: {error_str}")
            result['web_session_api'] = None
            result['inconclusive'] = True
            if result['valid']:
                result['relogin_recommended'] = False
            result['details'].append(f"网页登录态: 验证异常，结果不确定 - {error_str[:50]}")

        try:
            logger.info(f"【{self.account_id}】测试图片上传API（使用测试图片实际上传）...")

            import os
            from PIL import Image

            temp_dir = tempfile.gettempdir()
            test_image_path = os.path.join(temp_dir, f'cookie_test_{self.account_id}.png')

            try:
                img = Image.new('RGB', (1, 1), color='white')
                img.save(test_image_path, 'PNG')
                logger.info(f"【{self.account_id}】已创建测试图片: {test_image_path}")

                from utils.image_uploader import ImageUploader
                uploader = ImageUploader(cookies_str=self.cookies_str)

                await uploader.create_session()

                try:
                    upload_result = None
                    error_type = None
                    error_message = None

                    for attempt in range(2):
                        upload_result = await uploader.upload_image(test_image_path)
                        if upload_result:
                            break

                        error_type = getattr(uploader, 'last_error_type', None)
                        error_message = getattr(uploader, 'last_error_message', None) or "未知原因"
                        is_retryable_auth = error_type == 'auth' and error_message == '返回登录页面' and result['web_session_api'] is not False
                        if attempt == 0 and is_retryable_auth:
                            logger.warning(
                                f"【{self.account_id}】图片上传校验首次返回登录页，但网页登录态仍可访问，1.5秒后重试一次"
                            )
                            await asyncio.sleep(1.5)
                            continue
                        break
                finally:
                    await uploader.close_session()

                if upload_result:
                    logger.info(f"【{self.account_id}】✅ 图片上传API验证通过: 上传成功 ({upload_result[:50]}...)")
                    result['image_api'] = True
                    result['details'].append("图片上传API: 通过验证")
                else:
                    error_type = getattr(uploader, 'last_error_type', None)
                    error_message = getattr(uploader, 'last_error_message', None) or "未知原因"
                    if error_type == 'network':
                        logger.warning(f"【{self.account_id}】⚠️ 图片上传API验证遇到网络异常，不判定为Cookie失效: {error_message}")
                        result['image_api'] = None
                        result['inconclusive'] = True
                        if result['valid']:
                            result['relogin_recommended'] = False
                        result['details'].append(f"图片上传API: 网络异常，结果不确定 ({error_message[:50]})")
                    elif error_type == 'http' and getattr(uploader, 'last_http_status', None) and uploader.last_http_status >= 500:
                        logger.warning(f"【{self.account_id}】⚠️ 图片上传API返回服务端异常，不判定为Cookie失效: HTTP {uploader.last_http_status}")
                        result['image_api'] = None
                        result['inconclusive'] = True
                        if result['valid']:
                            result['relogin_recommended'] = False
                        result['details'].append(f"图片上传API: 服务端异常，结果不确定 (HTTP {uploader.last_http_status})")
                    elif error_type == 'auth' and error_message == '返回登录页面':
                        logger.warning(
                            f"【{self.account_id}】❌ 图片上传接口返回登录页，按旧版严格策略判定Cookie失效"
                        )
                        result['image_api'] = False
                        result['valid'] = False
                        result['details'].append("图片上传API: 返回登录页面")
                    else:
                        logger.warning(f"【{self.account_id}】❌ 图片上传API验证失败: {error_message}")
                        result['image_api'] = False
                        result['valid'] = False
                        result['details'].append(f"图片上传API: {error_message[:50]}")

            finally:
                if os.path.exists(test_image_path):
                    try:
                        os.remove(test_image_path)
                        logger.debug(f"【{self.account_id}】已删除测试图片")
                    except Exception:
                        pass

        except Exception as e:
            error_str = self._safe_str(e)
            logger.error(f"【{self.account_id}】图片上传API验证异常: {error_str}")
            error_lower = error_str.lower()
            auth_keywords = ['返回登录页面', 'session过期', '令牌过期', 'login', 'mini_login', 'passport.goofish.com']
            if any(keyword.lower() in error_lower for keyword in auth_keywords):
                result['image_api'] = False
                result['valid'] = False
                result['details'].append(f"图片上传API: 验证异常({error_str[:50]})")
            else:
                result['image_api'] = None
                result['inconclusive'] = True
                if result['valid']:
                    result['relogin_recommended'] = False
                result['details'].append(f"图片上传API: 验证异常，结果不确定 - {error_str[:50]}")

        if result['image_api'] is False:
            result['valid'] = False
        elif result['web_session_api'] is False and result['image_api'] is not True:
            result['valid'] = False
        elif result['web_session_api'] is False and result['image_api'] is True:
            logger.warning(f"【{self.account_id}】❌ 网页登录态与图片上传校验结果不一致，按严格策略判定Cookie失效")
            result['valid'] = False
            result['details'].append("校验结果: 网页登录态与图片上传结果不一致")

        if result['valid']:
            if result['inconclusive']:
                logger.warning(f"【{self.account_id}】⚠️ Cookie验证结果不确定: 未发现明确失效证据，但部分校验存在波动或结果矛盾")
            else:
                logger.info(f"【{self.account_id}】✅ Cookie验证通过: 所有关键API均可用")
        else:
            logger.warning(f"【{self.account_id}】❌ Cookie验证失败:")
            for detail in result['details']:
                logger.warning(f"【{self.account_id}? - {detail}")

        result['details'] = '; '.join(result['details'])
        return result

    async def _restart_instance(self):
        try:
            current_account_id = self._canonical_account_id()
            log_account_id = current_account_id or "default"
            logger.info(f"【{log_account_id}】准备重启实例...")

            from cookie_manager import manager as cookie_manager

            if not current_account_id:
                logger.warning(f"【{log_account_id}】实例未绑定有效 account_id，拒绝触发重启")
                return

            if cookie_manager:
                logger.info(f"【{log_account_id}】通过CookieManager重启实例...")

                # 延迟触发实例重启，避免与当前处理流程直接竞争。
                import threading

                def trigger_restart():
                    try:
                        import time
                        time.sleep(2.0)

                        # update_config_cookies 已落库，这里只触发重启，不重复保存。
                        cookie_manager.update_cookie(current_account_id, self.cookies_str, save_to_db=False)
                        logger.info(f"【{log_account_id}】实例重启请求已触发")
                    except Exception as e:
                        logger.error(f"【{log_account_id}】触发实例重启失败: {e}")
                        import traceback
                        logger.error(f"【{log_account_id}】重启失败详情:\n{traceback.format_exc()}")

                restart_thread = threading.Thread(target=trigger_restart, daemon=True)
                restart_thread.start()

                logger.info(f"【{log_account_id}】实例重启已触发，当前任务即将退出...")
                logger.warning(f"【{log_account_id}】注意：重启请求已发送，CookieManager将在2秒后取消当前任务并启动新实例")

            else:
                logger.warning(f"【{log_account_id}】CookieManager不可用，无法重启实例")

        except Exception as e:
            logger.error(f"【{current_account_id or 'default'}】重启实例失败: {self._safe_str(e)}")
            import traceback
            logger.error(f"【{current_account_id or 'default'}】重启失败堆栈:\n{traceback.format_exc()}")
            try:
                await self.send_token_refresh_notification(f"实例重启失败: {str(e)}", "instance_restart_failed")
            except Exception as notify_e:
                logger.error(f"【{current_account_id or 'default'}】发送重启失败通知时出错: {self._safe_str(notify_e)}")

    async def save_item_info_to_db(self, item_id: str, item_detail: str = None, item_title: str = None):
        try:
            if item_id and item_id.startswith('auto_'):
                logger.warning(f"跳过保存自动生成的商品ID: {item_id}")
                return

            if not item_title and not item_detail:
                logger.warning(f"跳过保存商品信息：缺少商品标题和详情 - {item_id}")
                return

            if not item_title or not item_detail:
                logger.warning(f"跳过保存商品信息：商品标题或详情不完整 - {item_id}")
                return

            from db_manager import db_manager
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error(
                    f"【default】商品信息保存缺少 canonical account_id，拒绝继续运行 item_id={item_id}"
                )
                return

            item_data = item_detail

            success = db_manager.save_item_info(current_account_id, item_id, item_data)
            if success:
                logger.info(f"商品信息已保存到数据库: {item_id}")
            else:
                logger.warning(f"保存商品信息到数据库失败: {item_id}")

        except Exception as e:
            logger.error(f"保存商品信息到数据库异常: {self._safe_str(e)}")

    async def save_item_detail_only(self, item_id, item_detail):
        try:
            from db_manager import db_manager
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error(
                    f"【default】商品详情保存缺少 canonical account_id，拒绝继续运行 item_id={item_id}"
                )
                return False

            success = db_manager.update_item_detail(current_account_id, item_id, item_detail)

            if success:
                logger.info(f"商品详情已更新: {item_id}")
            else:
                logger.warning(f"更新商品详情失败: {item_id}")

            return success

        except Exception as e:
            logger.error(f"更新商品详情异常: {self._safe_str(e)}")
            return False

    async def fetch_item_detail_from_api(self, item_id: str, force_refresh: bool = False) -> str:
        try:
            auto_fetch_config = config.get('ITEM_DETAIL', {}).get('auto_fetch', {})

            if not auto_fetch_config.get('enabled', True):
                logger.warning(f"自动获取商品详情功能已禁用: {item_id}")
                return ""

            if not force_refresh:
                async with self._item_detail_cache_lock:
                    if item_id in self._item_detail_cache:
                        cache_data = self._item_detail_cache[item_id]
                        cache_time = cache_data['timestamp']
                        current_time = time.time()

                        if current_time - cache_time < self._item_detail_cache_ttl:
                            logger.info(f"从缓存获取商品详情: {item_id}")
                            return cache_data['detail']
                        else:
                            logger.warning(f"缓存已过期，删除: {item_id}")
            else:
                logger.info(f"强制刷新商品详情，跳过缓存: {item_id}")

            detail_from_browser = await self._fetch_item_detail_from_browser(item_id)
            if detail_from_browser:
                await self._add_to_item_cache(item_id, detail_from_browser)
                logger.info(f"成功通过浏览器获取商品详情: {item_id}, 长度: {len(detail_from_browser)}")
                return detail_from_browser

            logger.warning(f"浏览器获取商品详情失败: {item_id}")
            return ""

        except Exception as e:
            logger.error(f"获取商品详情异常: {item_id}, 错误: {self._safe_str(e)}")
            return ""

    async def _add_to_item_cache(self, item_id: str, detail: str):
        async with self._item_detail_cache_lock:
            current_time = time.time()

            if len(self._item_detail_cache) >= self._item_detail_cache_max_size:
                if self._item_detail_cache:
                    oldest_item = min(
                        self._item_detail_cache.items(),
                        key=lambda x: x[1].get('access_time', x[1]['timestamp'])
                    )
                    oldest_item_id = oldest_item[0]
                    del self._item_detail_cache[oldest_item_id]
                    logger.warning(f"缓存已满，删除最旧项: {oldest_item_id}")

            self._item_detail_cache[item_id] = {
                'detail': detail,
                'timestamp': current_time,
                'access_time': current_time
            }
            logger.warning(f"添加商品详情到缓存: {item_id}, 当前缓存大小: {len(self._item_detail_cache)}")

    @classmethod
    async def _cleanup_item_cache(cls):
        try:
            async with cls._item_detail_cache_lock:
                await asyncio.sleep(0)

                current_time = time.time()
                expired_items = []

                for item_id, cache_data in cls._item_detail_cache.items():
                    await asyncio.sleep(0)
                    if current_time - cache_data['timestamp'] >= cls._item_detail_cache_ttl:
                        expired_items.append(item_id)

                for item_id in expired_items:
                    await asyncio.sleep(0)

                if expired_items:
                    logger.info(f"清理了 {len(expired_items)} 个过期的商品详情缓存")

                return len(expired_items)
        except asyncio.CancelledError:
            raise

    async def _fetch_item_detail_from_browser(self, item_id: str) -> str:
        browser = None
        context = None
        page = None
        runtime_lease = None
        release_reason = "item_detail_fetch_failed"
        try:
            logger.info(f"开始使用浏览器获取商品详情: {item_id}")

            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error(f"商品详情抓取缺少 account_id，无法申请账号级 runtime: {item_id}")
                return ""

            profile_dir = account_browser_runtime_manager.resolve_profile_dir(current_account_id)
            browser_args = self._build_browser_refresh_launch_args()
            context_options = dict(self._build_browser_refresh_context_options())
            runtime_lease = await account_browser_runtime_manager.acquire_runtime(
                current_account_id,
                "item_detail_fetch",
                exclusive=False,
                runtime_request={
                    "account_id": current_account_id,
                    "purpose": "item_detail_fetch",
                    "headless": True,
                    "use_persistent_context": True,
                    "profile_dir": profile_dir,
                    "launch_options": {
                        "headless": True,
                        "args": browser_args,
                    },
                    "context_options": context_options,
                },
            )
            page, context = await account_browser_runtime_manager.get_fresh_page(runtime_lease)
            runtime = getattr(runtime_lease, "runtime", None)
            browser = getattr(runtime, "browser", None) or getattr(context, "browser", None)

            item_url = f"https://www.goofish.com/item?id={item_id}"
            logger.info(f"访问商品页面: {item_url}")

            await page.goto(item_url, wait_until='domcontentloaded', timeout=30000)

            await asyncio.sleep(2)

            detail_text = ""
            try:
                selectors = [
                    '.detailDesc--descText--1FMDTCm',
                    'span.rax-text-v2.detailDesc--descText--1FMDTCm',
                    '[class*="detailDesc--descText"]',
                    '[class*="descText"]',
                    '.desc--GaIUKUQY',
                    '.detail-desc',
                    '.item-desc',
                    '[class*="desc"]',
                    ]

                for selector in selectors:
                    try:
                        await page.wait_for_selector(selector, timeout=3000)
                        detail_element = await page.query_selector(selector)
                        if detail_element:
                            detail_text = await detail_element.inner_text()
                            if detail_text and len(detail_text.strip()) > 0:
                                logger.info(f"成功获取商品详情（选择器: {selector}）: {item_id}, 长度: {len(detail_text)}")
                                release_reason = "item_detail_fetch_completed"
                                return detail_text.strip()
                    except Exception as e:
                        logger.debug(f"选择器 {selector} 未找到: {self._safe_str(e)}")
                        continue

                logger.warning(f"未找到特定详情元素，尝试获取整个页面内容: {item_id}")
                body_text = await page.inner_text('body')
                if body_text:
                    logger.info(f"获取到页面整体内容: {item_id}, 长度: {len(body_text)}")
                    release_reason = "item_detail_fetch_completed"
                    return body_text.strip()
                else:
                    logger.warning(f"未找到商品详情元素: {item_id}")

            except Exception as e:
                logger.warning(f"获取商品详情元素失败: {item_id}, 错误: {self._safe_str(e)}")

            release_reason = "item_detail_fetch_completed"
            return ""

        except Exception as e:
            logger.error(f"浏览器获取商品详情异常: {item_id}, 错误: {self._safe_str(e)}")
            return ""
        finally:
            try:
                if runtime_lease is not None or browser or context or page:
                    await self._release_browser_recovery_runtime(
                        runtime_lease,
                        browser=browser,
                        context=context,
                        page=page,
                        reason=release_reason,
                    )
                    logger.warning(f"浏览器资源已关闭: {item_id}")
            except Exception as e:
                logger.warning(f"关闭浏览器资源时出错: {self._safe_str(e)}")


    async def save_items_list_to_db(self, items_list, sync_item_details=False):
        try:
            from db_manager import db_manager
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error("【default】批量保存商品信息缺少 canonical account_id，拒绝继续运行")
                return 0

            batch_new_data = []
            batch_update_data = []
            items_need_detail = []
            for item in items_list:
                item_id = item.get('id')
                if not item_id or item_id.startswith('auto_'):
                    continue

                item_detail = {
                    'title': item.get('title', ''),
                    'price': item.get('price', ''),
                    'price_text': item.get('price_text', ''),
                    'category_id': item.get('category_id', ''),
                    'auction_type': item.get('auction_type', ''),
                    'item_status': item.get('item_status', 0),
                    'detail_url': item.get('detail_url', ''),
                    'pic_info': item.get('pic_info', {}),
                    'detail_params': item.get('detail_params', {}),
                    'track_params': item.get('track_params', {}),
                    'item_label_data': item.get('item_label_data', {}),
                    'card_type': item.get('card_type', 0)
                }

                existing_item = db_manager.get_item_info(current_account_id, item_id)

                if existing_item:
                    batch_update_data.append({
                        'account_id': current_account_id,
                        'item_id': item_id,
                        'item_title': item.get('title', ''),
                        'item_price': item.get('price_text', ''),
                        'item_category': str(item.get('category_id', ''))
                    })
                    if sync_item_details:
                        items_need_detail.append({
                            'item_id': item_id,
                            'item_title': item.get('title', '')
                        })
                    logger.debug(f"商品 {item_id} 已存在，将更新标题和价格")
                else:
                    batch_new_data.append({
                        'account_id': current_account_id,
                        'item_id': item_id,
                        'item_title': item.get('title', ''),
                        'item_description': '',
                        'item_category': str(item.get('category_id', '')),
                        'item_price': item.get('price_text', ''),
                        'item_detail': json.dumps(item_detail, ensure_ascii=False)
                    })

                    items_need_detail.append({
                        'item_id': item_id,
                        'item_title': item.get('title', '')
                    })
                    logger.debug(f"商品 {item_id} 是新商品，将保存完整信息")

            saved_count = 0

            if batch_new_data:
                new_count = db_manager.batch_save_item_basic_info(batch_new_data)
                logger.info(f"新增商品信息: {new_count}/{len(batch_new_data)} 个")
                saved_count += new_count

            if batch_update_data:
                update_count = db_manager.batch_update_item_title_price(batch_update_data)
                logger.info(f"更新商品标题和价格: {update_count}/{len(batch_update_data)} 个")
                saved_count += update_count

            if items_need_detail:
                from config import config
                auto_fetch_config = config.get('ITEM_DETAIL', {}).get('auto_fetch', {})

                if auto_fetch_config.get('enabled', True):
                    action_text = '同步最新详情' if sync_item_details else '获取缺失详情'
                    logger.info(f"准备为 {len(items_need_detail)} 个商品{action_text}...")
                    detail_success_count = await self._fetch_item_details(
                        items_need_detail,
                        force_refresh=sync_item_details,
                    )
                    logger.info(f"成功为 {detail_success_count}/{len(items_need_detail)} 个商品{action_text}")
                else:
                    logger.info(f"有 {len(items_need_detail)} 个商品需要获取详情，但自动获取功能已禁用")

            return saved_count

        except Exception as e:
            logger.error(f"批量保存商品信息异常: {self._safe_str(e)}")
            return 0

    async def _fetch_item_details(self, items_need_detail, force_refresh=False):
        success_count = 0

        try:
            from db_manager import db_manager
            from config import config

            auto_fetch_config = config.get('ITEM_DETAIL', {}).get('auto_fetch', {})
            max_concurrent = auto_fetch_config.get('max_concurrent', 3)
            retry_delay = auto_fetch_config.get('retry_delay', 0.5)

            semaphore = asyncio.Semaphore(max_concurrent)

            async def fetch_single_item_detail(item_info):
                async with semaphore:
                    try:
                        item_id = item_info['item_id']
                        item_title = item_info['item_title']

                        item_detail_text = await self.fetch_item_detail_from_api(
                            item_id,
                            force_refresh=force_refresh,
                        )

                        if item_detail_text:
                            success = await self.save_item_detail_only(item_id, item_detail_text)
                            if success:
                                logger.info(f"✅ 成功获取并保存商品详情: {item_id} - {item_title}")
                                return 1
                            else:
                                logger.warning(f"❌ 获取详情成功但保存失败: {item_id}")
                        else:
                            logger.warning(f"❌ 未能获取商品详情: {item_id} - {item_title}")

                        await asyncio.sleep(retry_delay)
                        return 0

                    except Exception as e:
                        logger.error(f"获取单个商品详情异常: {item_info.get('item_id', 'unknown')}, 错误: {self._safe_str(e)}")
                        return 0

            tasks = [fetch_single_item_detail(item_info) for item_info in items_need_detail]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, int):
                    success_count += result
                elif isinstance(result, Exception):
                    logger.error(f"获取商品详情任务异常: {result}")

            return success_count

        except Exception as e:
            logger.error(f"批量获取商品详情异常: {self._safe_str(e)}")
            return success_count

    async def get_item_info(self, item_id, retry_count=0):
        if retry_count >= 4:
            logger.error("获取商品信息失败，重试次数过多")
            return {"error": "获取商品信息失败，重试次数过多"}

        if not self.session:
            await self.create_session()

        params = {
            'jsv': '2.7.2',
            'appKey': '34839810',
            't': str(int(time.time()) * 1000),
            'sign': '',
            'v': '1.0',
            'type': 'originaljson',
            'accountSite': 'xianyu',
            'dataType': 'json',
            'timeout': '20000',
            'api': 'mtop.taobao.idle.pc.detail',
            'sessionOption': 'AutoLoginOnly',
            'spm_cnt': 'a21ybx.im.0.0',
        }

        data_val = '{"itemId":"' + item_id + '"}'
        data = {
            'data': data_val,
        }

        token = trans_cookies(self.cookies_str).get('_m_h5_tk', '').split('_')[0] if trans_cookies(self.cookies_str).get('_m_h5_tk') else ''

        if token:
            logger.warning(f"使用cookies中的_m_h5_tk token: {self._mask_secret_value(token, head=6, tail=4)}")
        else:
            logger.warning("cookies中没有找到_m_h5_tk token")

        from utils.xianyu_utils import generate_sign
        sign = generate_sign(params['t'], token, data_val)
        params['sign'] = sign

        try:
            async with self.session.post(
                'https://h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail/1.0/',
                params=params,
                data=data
            ) as response:
                res_json = await response.json()

                if await self._apply_response_cookie_updates(response.headers, "item_detail"):
                    logger.warning("已更新Cookie到数据库")

                logger.warning(f"商品信息获取成功: {res_json}")
                if isinstance(res_json, dict):
                    ret_value = res_json.get('ret', [])
                    if not any('SUCCESS::调用成功' in ret for ret in ret_value):
                        logger.warning(f"商品信息API调用失败，错误信息: {ret_value}")

                        await asyncio.sleep(0.5)
                        return await self.get_item_info(item_id, retry_count + 1)
                    else:
                        logger.warning(f"商品信息获取成功: {item_id}")
                        return res_json
                else:
                    logger.error(f"商品信息API返回格式异常: {res_json}")
                    return await self.get_item_info(item_id, retry_count + 1)

        except Exception as e:
            logger.error(f"商品信息API请求异常: {self._safe_str(e)}")
            await asyncio.sleep(0.5)
            return await self.get_item_info(item_id, retry_count + 1)

    def extract_item_id_from_message(self, message):
        try:

            message_3 = message.get('3', {})
            if isinstance(message_3, dict):

                if 'extension' in message_3:
                    extension = message_3['extension']
                    if isinstance(extension, dict):
                        item_id = extension.get('itemId') or extension.get('item_id')
                        if item_id:
                            logger.info(f"从extension中提取商品ID: {item_id}")
                            return item_id

                if 'bizData' in message_3:
                    biz_data = message_3['bizData']
                    if isinstance(biz_data, dict):
                        item_id = biz_data.get('itemId') or biz_data.get('item_id')
                        if item_id:
                            logger.info(f"从bizData中提取商品ID: {item_id}")
                            return item_id

                for key, value in message_3.items():
                    if isinstance(value, dict):
                        item_id = value.get('itemId') or value.get('item_id')
                        if item_id:
                            logger.info(f"从{key}字段中提取商品ID: {item_id}")
                            return item_id

                content = message_3.get('content', '')
                if isinstance(content, str) and content:
                    id_match = re.search(r'(\d{10,})', content)
                    if id_match:
                        logger.info(f"【{self.account_id}】从消息内容中提取商品ID: {id_match.group(1)}")
                        return id_match.group(1)

            skip_keys = {'1', 'tradeId', 'trade_id', 'bizId', 'biz_id', 'orderId', 'order_id',
                        'userId', 'user_id', 'senderId', 'sender_id', 'receiverId', 'receiver_id',
                        'chatId', 'chat_id', 'conversationId', 'conversation_id', 'msgId', 'msg_id'}

            def find_item_id_recursive(obj, path=""):
                if isinstance(obj, dict):
                    for key in ['itemId', 'item_id']:
                        if key in obj and isinstance(obj[key], (str, int)):
                            value = str(obj[key])
                            if len(value) >= 10 and value.isdigit():
                                logger.info(f"从{path}.{key}中提取商品ID: {value}")
                                return value

                    for key, value in obj.items():
                        if key in skip_keys:
                            continue
                        result = find_item_id_recursive(value, f"{path}.{key}" if path else key)
                        if result:
                            return result

                elif isinstance(obj, str):
                    if '@goofish' in obj or '@xianyu' in obj:
                        return None
                    if 'itemId=' in obj:
                        id_match = re.search(r'itemId=(\d{10,})', obj)
                        if id_match:
                            logger.info(f"从{path}的URL参数中提取商品ID: {id_match.group(1)}")
                            return id_match.group(1)

                return None

            result = find_item_id_recursive(message)
            if result:
                return result

            logger.warning("扢有方法都未能提取到商品ID")
            return None

        except Exception as e:
            logger.error(f"提取商品ID失败: {self._safe_str(e)}")
            return None

    def debug_message_structure(self, message, context=""):
        try:
            logger.warning(f"[{context}] 消息结构调试:")
            logger.warning(f"  消息类型: {type(message)}")

            if isinstance(message, dict):
                for key, value in message.items():
                    logger.warning(f"  键 '{key}': {type(value)} - {str(value)[:100]}...")

                    if key in ["1", "3"] and isinstance(value, dict):
                        logger.warning(f"    详细结构 '{key}':")
                        for sub_key, sub_value in value.items():
                            logger.warning(f"      '{sub_key}': {type(sub_value)} - {str(sub_value)[:50]}...")
            else:
                logger.warning(f"  消息内容: {str(message)[:200]}...")

        except Exception as e:
            logger.error(f"调试消息结构时发生错误: {self._safe_str(e)}")

    async def get_item_specific_reply(self, send_user_name: str, send_user_id: str, send_message: str, item_id: str = None) -> str:
        if not item_id:
            return None

        try:
            from db_manager import db_manager
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error("【default】指定商品回复读取缺少 canonical account_id，拒绝继续运行")
                return None

            item_reply = db_manager.get_item_reply(current_account_id, item_id)
            if not item_reply or not item_reply.get('reply_content'):
                return None

            reply_content = item_reply['reply_content']
            logger.info(f"【{self.account_id}】使用指定商品回复: 商品ID={item_id}")

            try:
                formatted_reply = reply_content.format(
                    send_user_name=send_user_name,
                    send_user_id=send_user_id,
                    send_message=send_message,
                    item_id=item_id
                )
                logger.info(f"【{self.account_id}】指定商品回复内容: {formatted_reply}")
                return formatted_reply
            except Exception as format_error:
                logger.error(f"指定商品回复变量替换失败: {self._safe_str(format_error)}")
                return reply_content

        except Exception as e:
            logger.error(f"获取指定商品回复失败: {self._safe_str(e)}")
            return None

    async def get_default_reply(self, send_user_name: str, send_user_id: str, send_message: str, chat_id: str, item_id: str = None) -> str:
        try:
            from db_manager import db_manager
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error("【default】默认回复读取缺少 canonical account_id，拒绝继续运行")
                return None

            default_reply_settings = db_manager.get_default_reply(current_account_id)

            if not default_reply_settings or not default_reply_settings.get('enabled', False):
                logger.warning(f"账号 {current_account_id} 未启用默认回复")
                return None

            if default_reply_settings.get('reply_once', False) and chat_id:
                if db_manager.has_default_reply_record(current_account_id, chat_id):
                    logger.info(f"【{current_account_id}】chat_id {chat_id} 已使用过默认回复，跳过（只回复一次）")
                    return "SKIP_REPLY"

            reply_content = default_reply_settings.get('reply_content', '')
            if not reply_content or (reply_content and reply_content.strip() == ''):
                logger.info(f"账号 {current_account_id} 默认回复内容为空，不进行回复")
                return "EMPTY_REPLY"
            try:
                formatted_reply = reply_content.format(
                    send_user_name=send_user_name,
                    send_user_id=send_user_id,
                    send_message=send_message
                )

                logger.info(f"【{current_account_id}】使用默认回复: {formatted_reply}")
                return formatted_reply
            except Exception as format_error:
                logger.error(f"默认回复变量替换失败: {self._safe_str(format_error)}")
                return reply_content

        except Exception as e:
            logger.error(f"获取默认回复失败: {self._safe_str(e)}")
            return None

    async def get_keyword_reply(self, send_user_name: str, send_user_id: str, send_message: str, item_id: str = None) -> str:
        try:
            from db_manager import db_manager
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error("【default】关键词回复读取缺少 canonical account_id，拒绝继续运行")
                return None

            keywords = db_manager.get_keywords_with_type(current_account_id)

            if not keywords:
                logger.warning(f"账号 {current_account_id} 没有配置关键词")
                return None

            if item_id:
                for keyword_data in keywords:
                    keyword = keyword_data['keyword']
                    reply = keyword_data['reply']
                    keyword_item_id = keyword_data['item_id']
                    keyword_type = keyword_data.get('type', 'text')
                    image_url = keyword_data.get('image_url')

                    if keyword_item_id == item_id and keyword.lower() in send_message.lower():
                        logger.info(f"商品ID关键词匹配成功: 商品{item_id} '{keyword}' (类型: {keyword_type})")

                        if keyword_type == 'image' and image_url:
                            return await self._handle_image_keyword(keyword, image_url, send_user_name, send_user_id, send_message)
                        else:
                            if not reply or (reply and reply.strip() == ''):
                                logger.info(f"商品ID关键词 '{keyword}' 回复内容为空，不进行回复")
                                return "EMPTY_REPLY"
                            try:
                                formatted_reply = reply.format(
                                    send_user_name=send_user_name,
                                    send_user_id=send_user_id,
                                    send_message=send_message
                                )
                                logger.info(f"商品ID文本关键词回复: {formatted_reply}")
                                return formatted_reply
                            except Exception as format_error:
                                logger.error(f"关键词回复变量替换失败: {self._safe_str(format_error)}")
                                return reply

            for keyword_data in keywords:
                keyword = keyword_data['keyword']
                reply = keyword_data['reply']
                keyword_item_id = keyword_data['item_id']
                keyword_type = keyword_data.get('type', 'text')
                image_url = keyword_data.get('image_url')

                if not keyword_item_id and keyword.lower() in send_message.lower():
                    logger.info(f"通用关键词匹配成功: '{keyword}' (类型: {keyword_type})")

                    if keyword_type == 'image' and image_url:
                        return await self._handle_image_keyword(keyword, image_url, send_user_name, send_user_id, send_message)
                    else:
                        if not reply or (reply and reply.strip() == ''):
                            logger.info(f"通用关键词 '{keyword}' 回复内容为空，不进行回复")
                            return "EMPTY_REPLY"
                        try:
                            formatted_reply = reply.format(
                                send_user_name=send_user_name,
                                send_user_id=send_user_id,
                                send_message=send_message
                            )
                            logger.info(f"通用文本关键词回复: {formatted_reply}")
                            return formatted_reply
                        except Exception as format_error:
                            logger.error(f"关键词回复变量替换失败: {self._safe_str(format_error)}")
                            return reply

            logger.warning(f"未找到匹配的关键词: {send_message}")
            return None

        except Exception as e:
            logger.error(f"获取关键词回复失败: {self._safe_str(e)}")
            return None

    async def _handle_image_keyword(self, keyword: str, image_url: str, send_user_name: str, send_user_id: str, send_message: str) -> str:
        try:
            if self._is_cdn_url(image_url):
                logger.info(f"使用已有的CDN图片链接: {image_url}")
                return f"__IMAGE_SEND__{image_url}"

            elif image_url.startswith('/static/uploads/') or image_url.startswith('static/uploads/'):
                local_image_path = image_url.replace('/static/uploads/', 'static/uploads/')
                if os.path.exists(local_image_path):
                    logger.info(f"准备上传本地图片到闲鱼CDN: {local_image_path}")

                    from utils.image_uploader import ImageUploader
                    uploader = ImageUploader(self.cookies_str)

                    async with uploader:
                        cdn_url = await uploader.upload_image(local_image_path)
                        if cdn_url:
                            logger.info(f"图片上传成功，CDN URL: {cdn_url}")
                            await self._update_keyword_image_url(keyword, cdn_url)
                            image_url = cdn_url
                        else:
                            logger.error(f"图片上传失败: {local_image_path}")
                            logger.error(f"❌ Cookie可能已失效！请检查配置并更新Cookie")
                            return f"抱歉，图片发送失败（Cookie可能已失效，请检查日志）"
                else:
                    logger.error(f"本地图片文件不存在: {local_image_path}")
                    return f"抱歉，图片文件不存在。"

            else:
                logger.info(f"使用外部图片链接: {image_url}")

            return f"__IMAGE_SEND__{image_url}"

        except Exception as e:
            logger.error(f"处理图片关键词失败: {e}")
            return f"抱歉，图片发送失败: {str(e)}"

    def _is_cdn_url(self, url: str) -> bool:
        if not url:
            return False

        cdn_domains = [
            'gw.alicdn.com',
            'img.alicdn.com',
            'cloud.goofish.com',
            'goofish.com',
            'taobaocdn.com',
            'tbcdn.cn',
            'aliimg.com'
        ]

        url_lower = url.lower()
        for domain in cdn_domains:
            if domain in url_lower:
                return True

        if url_lower.startswith('https://') and any(ext in url_lower for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
            return True

        return False

    async def _update_keyword_image_url(self, keyword: str, new_image_url: str):
        try:
            from db_manager import db_manager
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error(
                    f"【default】关键词图片更新缺少 canonical account_id，拒绝继续运行 keyword={keyword}"
                )
                return
            success = db_manager.update_keyword_image_url(
                current_account_id,
                keyword,
                new_image_url,
            )
            if success:
                logger.info(f"图片URL已更新: {keyword} -> {new_image_url}")
            else:
                logger.warning(f"图片URL更新失败: {keyword}")
        except Exception as e:
            logger.error(f"更新关键词图片URL失败: {e}")

    async def _update_card_image_url(self, card_id: int, new_image_url: str):
        try:
            from db_manager import db_manager
            success = db_manager.update_card_image_url(card_id, new_image_url)
            if success:
                logger.info(f"卡券图片URL已更新: 卡券ID={card_id} -> {new_image_url}")
            else:
                logger.warning(f"卡券图片URL更新失败: 卡券ID={card_id}")
        except Exception as e:
            logger.error(f"更新卡券图片URL失败: {e}")

    async def get_ai_reply(self, send_user_name: str, send_user_id: str, send_message: str, item_id: str, chat_id: str):
        try:
            from ai_reply_engine import ai_reply_engine
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error("【default】AI回复缺少 canonical account_id，拒绝继续运行")
                return None

            if not ai_reply_engine.is_ai_enabled(current_account_id):
                logger.warning(f"账号 {self.account_id} 未启用AI回复")
                return None

            from db_manager import db_manager
            item_info_raw = db_manager.get_item_info(current_account_id, item_id)

            if not item_info_raw:
                logger.warning(f"数据库中无商品信息: {item_id}")
                item_info = {
                    'title': '商品信息获取失败',
                    'price': 0,
                    'desc': '暂无商品描述'
                }
            else:
                item_info = {
                    'title': item_info_raw.get('item_title', '未知商品'),
                    'price': self._parse_price(item_info_raw.get('item_price', '0')),
                    'desc': item_info_raw.get('item_detail', '暂无商品描述')
                }

            reply = ai_reply_engine.generate_reply(
                message=send_message,
                item_info=item_info,
                chat_id=chat_id,
                account_id=current_account_id,
                user_id=send_user_id,
                item_id=item_id,
                skip_wait=True
            )

            if reply:
                logger.info(f"【{self.account_id}】AI回复生成成功: {reply}")
                return reply
            else:
                logger.warning(f"AI回复生成失败")
                return None

        except Exception as e:
            logger.error(f"获取AI回复失败: {self._safe_str(e)}")
            return None

    def _parse_price(self, price_str: str) -> float:
        try:
            if not price_str:
                return 0.0
            price_clean = re.sub(r'[^\d.]', '', str(price_str))
            return float(price_clean) if price_clean else 0.0
        except Exception:
            return 0.0

    def _get_notification_template(self, template_type: str) -> str:
        return get_notification_template_text(template_type)

    def _format_template(self, template: str, **kwargs) -> str:
        return format_notification_template(template, **kwargs)

    async def send_notification(self, send_user_name: str, send_user_id: str, send_message: str, item_id: str = None, chat_id: str = None):
        try:
            import hashlib
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error("【default】消息通知缺少 canonical account_id，拒绝继续运行")
                return

            system_messages = [
                '发来丢条消',
                '发来丢条新消息'
            ]

            if send_message in system_messages:
                logger.warning(f"📱 系统消息不发送通知: {send_message}")
                return

            notification_key = f"{chat_id or 'unknown'}_{send_user_id}_{send_message}"
            notification_hash = hashlib.md5(notification_key.encode('utf-8')).hexdigest()
            reservation_key = f"msg:{notification_hash}"

            async with self.notification_lock:
                current_time = time.time()
                if notification_hash in self.last_notification_time:
                    time_since_last = current_time - self.last_notification_time[notification_hash]
                    if time_since_last < self.notification_cooldown:
                        remaining_seconds = int(self.notification_cooldown - time_since_last)
                        logger.warning(f"📱 通知在冷却期内（剩余 {remaining_seconds} 秒），跳过重复发送 - 账号: {self.account_id}, 买家: {send_user_name}, 消息: {send_message[:30]}...")
                        return
                if reservation_key in self.pending_notification_keys:
                    logger.warning(f"📱 相同消息通知正在发送中，跳过重复发送 - 账号: {self.account_id}, 买家: {send_user_name}")
                    return
                self.pending_notification_keys.add(reservation_key)

            try:
                logger.info(f"📱 开始发送消息通知 - 账号: {self.account_id}, 买家: {send_user_name}")

                notification_msg = render_notification_template(
                    'message',
                    account_id=current_account_id,
                    buyer_name=send_user_name,
                    buyer_id=send_user_id,
                    item_id=item_id or '未知',
                    chat_id=chat_id or '未知',
                    message=send_message,
                    time=time.strftime('%Y-%m-%d %H:%M:%S')
                )

                notification_sent = await dispatch_account_notifications(
                    current_account_id,
                    notification_msg,
                    title='接收消息通知',
                    notification_type='message',
                )

                if not notification_sent:
                    logger.warning(f"📱 消息通知未发送成功，不进入冷却 - 账号: {self.account_id}, 买家: {send_user_name}")
                    return

                async with self.notification_lock:
                    sent_time = time.time()
                    self.last_notification_time[notification_hash] = sent_time
                    expired_keys = [
                        key for key, timestamp in self.last_notification_time.items()
                        if sent_time - timestamp > 3600
                    ]
                    for key in expired_keys:
                        del self.last_notification_time[key]
            finally:
                async with self.notification_lock:
                    self.pending_notification_keys.discard(reservation_key)

        except Exception as e:
            logger.error(f"📱 处理消息通知失败: {self._safe_str(e)}")
            import traceback
            logger.error(f"📱 详细错误信息: {traceback.format_exc()}")

    def _parse_notification_config(self, config: str) -> dict:
        try:
            import json
            return json.loads(config)
        except (json.JSONDecodeError, TypeError):
            return {"config": config}

    async def _send_qq_notification(self, config_data: dict, message: str):
        try:
            import aiohttp

            logger.info(f"📱 QQ通知 - 开始处理配置数据: {config_data}")

            qq_number = config_data.get('qq_number') or config_data.get('config', '')
            qq_number = qq_number.strip() if qq_number else ''

            logger.info(f"📱 QQ通知 - 解析到QQ号码: {qq_number}")

            if not qq_number:
                logger.warning("📱 QQ通知 - QQ号码配置为空，无法发送通知")
                return False

            api_url = (
                (config_data.get('api_url') or '').strip()
                or (db_manager.get_system_setting('qq_notification_api_url') or '').strip()
                or str(os.getenv('QQ_NOTIFICATION_API_URL') or '').strip()
            )
            if not api_url:
                logger.warning("📱 QQ通知 - 未配置QQ通知API地址，已跳过发送")
                return False
            params = {
                'qq': qq_number,
                'msg': message
            }

            logger.info(f"📱 QQ通知 - 请求URL: {api_url}")
            logger.info(f"📱 QQ通知 - 请求参数: qq={qq_number}, msg长度={len(message)}")

            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, params=params, timeout=10) as response:
                    response_text = await response.text()
                    logger.info(f"📱 QQ通知 - 响应状态: {response.status}")

                    if response.status == 502:
                        logger.info(f"📱 QQ通知发送成功: {qq_number} (状态码: {response.status})")
                        return True
                    elif response.status == 200:
                        logger.info(f"📱 QQ通知发送成功: {qq_number} (状态码: {response.status})")
                        logger.warning(f"📱 QQ通知 - 响应内容: {response_text}")
                        return True
                    else:
                        logger.warning(f"📱 QQ通知发送失败: HTTP {response.status}")
                        logger.warning(f"📱 QQ通知 - 响应内容: {response_text}")
                        return False

        except Exception as e:
            logger.error(f"📱 发QQ通知异常: {self._safe_str(e)}")
            import traceback
            logger.error(f"📱 QQ通知异常详情: {traceback.format_exc()}")
            return False

    async def _send_dingtalk_notification(self, config_data: dict, message: str):
        try:
            import aiohttp
            import json
            import hmac
            import hashlib
            import base64
            import time

            webhook_url = config_data.get('webhook_url') or config_data.get('config', '')
            secret = config_data.get('secret', '')

            webhook_url = webhook_url.strip() if webhook_url else ''
            if not webhook_url:
                logger.warning("钉钉通知配置为空")
                return False

            if secret:
                timestamp = str(round(time.time() * 1000))
                secret_enc = secret.encode('utf-8')
                string_to_sign = f'{timestamp}\n{secret}'
                string_to_sign_enc = string_to_sign.encode('utf-8')
                hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
                sign = base64.b64encode(hmac_code).decode('utf-8')
                webhook_url += f'&timestamp={timestamp}&sign={sign}'

            data = {
                "msgtype": "markdown",
                "markdown": {
                    "title": "闲鱼管理系统通知",
                    "text": message
                }
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(webhook_url, json=data, timeout=10) as response:
                    if response.status == 200:
                        logger.info(f"钉钉通知发送成功")
                        return True
                    else:
                        logger.warning(f"钉钉通知发送失败: {response.status}")
                        return False

        except Exception as e:
            logger.error(f"发钉钉知异常: {self._safe_str(e)}")
            return False

    async def _send_feishu_notification(self, config_data: dict, message: str):
        try:
            import aiohttp
            import json
            import hmac
            import hashlib
            import base64

            logger.info(f"📱 飞书通知 - 开始处理配置数据: {config_data}")

            webhook_url = config_data.get('webhook_url', '')
            secret = config_data.get('secret', '')

            logger.info(f"📱 飞书通知 - Webhook URL: {webhook_url[:50]}...")
            logger.info(f"📱 飞书通知 - 是否有签名密钥: {'是' if secret else '否'}")

            if not webhook_url:
                logger.warning("📱 飞书通知 - Webhook URL配置为空，无法发送通知")
                return False

            timestamp = str(int(time.time()))
            sign = ""

            if secret:
                string_to_sign = f'{timestamp}\n{secret}'
                hmac_code = hmac.new(
                    string_to_sign.encode('utf-8'),
                    ''.encode('utf-8'),
                    digestmod=hashlib.sha256
                ).digest()
                sign = base64.b64encode(hmac_code).decode('utf-8')
                logger.info(f"📱 飞书通知 - 已生成签名")

            data = {
                "msg_type": "text",
                "content": {
                    "text": message
                },
                "timestamp": timestamp
            }

            if sign:
                data["sign"] = sign

            logger.info(f"📱 飞书通知 - 请求数据构建完成")

            async with aiohttp.ClientSession() as session:
                async with session.post(webhook_url, json=data, timeout=10) as response:
                    response_text = await response.text()
                    logger.info(f"📱 飞书通知 - 响应状态: {response.status}")
                    logger.info(f"📱 飞书通知 - 响应内容: {response_text}")

                    if response.status == 200:
                        try:
                            response_json = json.loads(response_text)
                            if response_json.get('code') == 0:
                                logger.info(f"📱 飞书通知发送成功")
                                return True
                            else:
                                logger.warning(f"📱 飞书通知发送失败: {response_json.get('msg', '未知错误')}")
                                return False
                        except json.JSONDecodeError:
                            logger.info(f"📱 飞书通知发送成功（响应格式异常）")
                            return True
                    else:
                        logger.warning(f"📱 飞书通知发送失败: HTTP {response.status}, 响应: {response_text}")
                        return False

        except Exception as e:
            logger.error(f"📱 发飞书知异常: {self._safe_str(e)}")
            import traceback
            logger.error(f"📱 飞书通知异常详情: {traceback.format_exc()}")
            return False

    async def _send_bark_notification(self, config_data: dict, message: str):
        try:
            import aiohttp
            import json
            from urllib.parse import quote

            logger.info(f"📱 Bark通知 - 开始处理配置数据: {config_data}")

            server_url = config_data.get('server_url', 'https://api.day.app').rstrip('/')
            device_key = config_data.get('device_key', '')
            title = config_data.get('title', '闲鱼管理系统通知')
            sound = config_data.get('sound', 'default')
            icon = config_data.get('icon', '')
            group = config_data.get('group', 'xianyu')
            url = config_data.get('url', '')

            logger.info(f"📱 Bark通知 - 服务器: {server_url}")
            logger.info(f"📱 Bark通知 - 设备密钥: {device_key[:10]}..." if device_key else "📱 Bark通知 - 设备密钥: 未设置")
            logger.info(f"📱 Bark通知 - 标题: {title}")

            if not device_key:
                logger.warning("📱 Bark通知 - 设备密钥配置为空，无法发送通知")
                return False

            api_url = f"{server_url}/push"

            data = {
                "device_key": device_key,
                "title": title,
                "body": message,
                "sound": sound,
                "group": group
            }

            if icon:
                data["icon"] = icon
            if url:
                data["url"] = url

            logger.info(f"📱 Bark通知 - API地址: {api_url}")
            logger.info(f"📱 Bark通知 - 请求数据构建完成")

            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, json=data, timeout=10) as response:
                    response_text = await response.text()
                    logger.info(f"📱 Bark通知 - 响应状态: {response.status}")
                    logger.info(f"📱 Bark通知 - 响应内容: {response_text}")

                    if response.status == 200:
                        try:
                            response_json = json.loads(response_text)
                            if response_json.get('code') == 200:
                                logger.info(f"📱 Bark通知发送成功")
                                return True
                            else:
                                logger.warning(f"📱 Bark通知发送失败: {response_json.get('message', '未知错误')}")
                                return False
                        except json.JSONDecodeError:
                            if 'success' in response_text.lower() or 'ok' in response_text.lower():
                                logger.info(f"📱 Bark通知发送成功")
                                return True
                            else:
                                logger.warning(f"📱 Bark通知响应格式异常: {response_text}")
                                return False
                    else:
                        logger.warning(f"📱 Bark通知发送失败: HTTP {response.status}, 响应: {response_text}")
                        return False

        except Exception as e:
            logger.error(f"📱 发Bark通知异常: {self._safe_str(e)}")
            import traceback
            logger.error(f"📱 Bark通知异常详情: {traceback.format_exc()}")
            return False

    async def _send_email_notification(self, config_data: dict, message: str, attachment_path: str = None):
        try:
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            from email.mime.image import MIMEImage
            import os

            smtp_server = config_data.get('smtp_server', '')
            smtp_port = int(config_data.get('smtp_port', 587))
            email_user = config_data.get('email_user', '')
            email_password = config_data.get('email_password', '')
            recipient_email = config_data.get('recipient_email', '')
            smtp_use_tls = config_data.get('smtp_use_tls', smtp_port == 587)
            if not all([smtp_server, email_user, email_password, recipient_email]):
                logger.warning("邮件通知配置不完整")
                return False

            msg = MIMEMultipart()
            msg['From'] = email_user
            msg['To'] = recipient_email
            msg['Subject'] = "闲鱼管理系统通知"

            msg.attach(MIMEText(message, 'plain', 'utf-8'))

            if attachment_path and os.path.exists(attachment_path):
                try:
                    with open(attachment_path, 'rb') as f:
                        img_data = f.read()

                    filename = os.path.basename(attachment_path)
                    if attachment_path.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                        img = MIMEImage(img_data)
                        img.add_header('Content-Disposition', 'attachment', filename=filename)
                        msg.attach(img)
                        logger.info(f"已添加图片附件: {filename}")
                    else:
                        from email.mime.application import MIMEApplication
                        attach = MIMEApplication(img_data)
                        attach.add_header('Content-Disposition', 'attachment', filename=filename)
                        msg.attach(attach)
                        logger.info(f"已添加附件: {filename}")
                except Exception as attach_error:
                    logger.error(f"添加邮件附件失败: {self._safe_str(attach_error)}")

            server = None
            try:
                if smtp_port == 465:
                    server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30)
                else:
                    server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
                    if smtp_use_tls:
                        server.starttls()

                try:
                    server.login(email_user, email_password)
                except smtplib.SMTPAuthenticationError as auth_error:
                    error_code = auth_error.smtp_code if hasattr(auth_error, 'smtp_code') else None
                    error_msg = str(auth_error)

                    logger.error(f"邮件SMTP认证失败 (错误码: {error_code})")
                    logger.error(f"邮箱地址: {email_user}")
                    logger.error(f"SMTP服务器: {smtp_server}:{smtp_port}")
                    logger.error(f"错误详情: {error_msg}")

                    suggestions = []
                    if 'qq.com' in email_user.lower() or 'qq' in smtp_server.lower():
                        suggestions.append("QQ邮箱需要使用授权码而不是登录密码")
                        suggestions.append("请到QQ邮箱设置 -> 账户 -> 开启SMTP服务 -> 生成授权码")
                    elif 'gmail.com' in email_user.lower() or 'gmail' in smtp_server.lower():
                        suggestions.append("Gmail需要使用应用专用密码")
                        suggestions.append("请到Google账户 -> 安全性 -> 两步验证 -> 应用专用密码")
                        suggestions.append("或启用'允许不够安全的应用访问'（不推荐）")
                    elif '163.com' in email_user.lower() or '126.com' in email_user.lower() or 'yeah.net' in email_user.lower():
                        suggestions.append("网易邮箱需要使用授权码")
                        suggestions.append("请到邮箱设置 -> POP3/SMTP/IMAP -> 开启SMTP服务 -> 生成授权码")
                    else:
                        suggestions.append("请检查邮箱密码/授权码是否正确")
                        suggestions.append("某些邮箱服务商需要使用授权码而不是登录密码")
                        suggestions.append("请查看邮箱服务商的SMTP设置说明")

                    if suggestions:
                        logger.error("解决建议:")
                        for i, suggestion in enumerate(suggestions, 1):
                            logger.error(f"  {i}. {suggestion}")

                    raise

                server.send_message(msg)
                logger.info(f"邮件通知发送成功: {recipient_email}")
                return True

            finally:
                if server:
                    try:
                        server.quit()
                    except Exception:
                        try:
                            server.close()
                        except Exception:
                            pass

        except smtplib.SMTPAuthenticationError:
            return False
        except smtplib.SMTPException as smtp_error:
            logger.error(f"SMTP协议错误: {self._safe_str(smtp_error)}")
            logger.error(f"SMTP服务器: {smtp_server}:{smtp_port}")
            logger.error(f"请检查SMTP服务器地址和端口配置是否正确")
            return False
        except Exception as e:
            logger.error(f"发邮件知异常: {self._safe_str(e)}")
            import traceback
            logger.error(f"邮件发送详细错误: {traceback.format_exc()}")
            return False

    async def _send_webhook_notification(self, config_data: dict, message: str):
        try:
            import aiohttp
            import json

            webhook_url = config_data.get('webhook_url', '')
            http_method = config_data.get('http_method', 'POST').upper()
            headers_str = config_data.get('headers', '{}')

            if not webhook_url:
                logger.warning("Webhook通知配置为空")
                return False

            try:
                custom_headers = json.loads(headers_str) if headers_str else {}
            except json.JSONDecodeError:
                custom_headers = {}

            headers = {'Content-Type': 'application/json'}
            headers.update(custom_headers)

            data = {
                'message': message,
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                'source': 'xianyu-auto-reply'
            }

            async with aiohttp.ClientSession() as session:
                if http_method == 'POST':
                    async with session.post(webhook_url, json=data, headers=headers, timeout=10) as response:
                        if response.status == 200:
                            logger.info(f"Webhook通知发送成功")
                            return True
                        else:
                            logger.warning(f"Webhook通知发送失败: {response.status}")
                            return False
                elif http_method == 'PUT':
                    async with session.put(webhook_url, json=data, headers=headers, timeout=10) as response:
                        if response.status == 200:
                            logger.info(f"Webhook通知发送成功")
                            return True
                        else:
                            logger.warning(f"Webhook通知发送失败: {response.status}")
                            return False
                else:
                    logger.warning(f"不支持的HTTP方法: {http_method}")
                    return False

        except Exception as e:
            logger.error(f"发Webhook通知异常: {self._safe_str(e)}")
            return False

    async def _send_wechat_notification(self, config_data: dict, message: str):
        try:
            import aiohttp
            import json

            webhook_url = config_data.get('webhook_url', '')

            if not webhook_url:
                logger.warning("微信通知配置为空")
                return False

            data = {
                "msgtype": "text",
                "text": {
                    "content": message
                }
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(webhook_url, json=data, timeout=10) as response:
                    if response.status == 200:
                        logger.info(f"微信通知发送成功")
                        return True
                    else:
                        logger.warning(f"微信通知发送失败: {response.status}")
                        return False

        except Exception as e:
            logger.error(f"发微信知异常: {self._safe_str(e)}")
            return False

    async def _send_telegram_notification(self, config_data: dict, message: str):
        try:
            import aiohttp

            bot_token = config_data.get('bot_token', '')
            chat_id = config_data.get('chat_id', '')

            if not all([bot_token, chat_id]):
                logger.warning("Telegram通知配置不完整")
                return False

            api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

            data = {
                'chat_id': chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, json=data, timeout=10) as response:
                    if response.status == 200:
                        logger.info(f"Telegram通知发送成功")
                        return True
                    else:
                        logger.warning(f"Telegram通知发送失败: {response.status}")
                        return False

        except Exception as e:
            logger.error(f"发Telegram通知异常: {self._safe_str(e)}")
            return False

    async def send_token_refresh_notification(self, error_message: str, notification_type: str = "token_refresh", chat_id: str = None, attachment_path: str = None, verification_url: str = None):
        try:
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error("【default】Token刷新通知缺少 canonical account_id，拒绝继续运行")
                return
            if notification_type != "token_scheduled_refresh_failed" and self._is_normal_token_expiry(error_message):
                logger.warning(f"棢测到正常的令牌过期，跳过通知: {error_message}")
                return

            is_token_related_error = self._is_token_related_error(error_message)
            notification_key = f"token:{notification_type}"

            if notification_type == "message_stream_stale":
                cooldown_time = self.message_stream_notification_cooldown
                cooldown_desc = f"{max(1, int(cooldown_time // 60))}分钟"
            elif is_token_related_error:
                cooldown_time = self.token_refresh_notification_cooldown
                cooldown_desc = "3小时"
            else:
                cooldown_time = self.notification_cooldown
                cooldown_desc = f"{self.notification_cooldown // 60}分钟"

            async with self.notification_lock:
                current_time = time.time()
                last_time = self.last_notification_time.get(notification_key, 0)
                if notification_key in self.pending_notification_keys:
                    logger.warning(f"Token刷新通知正在发送中，跳过重复发送: {notification_type}")
                    return
                if current_time - last_time < cooldown_time:
                    remaining_time = cooldown_time - (current_time - last_time)
                    remaining_hours = int(remaining_time // 3600)
                    remaining_minutes = int((remaining_time % 3600) // 60)
                    remaining_seconds = int(remaining_time % 60)

                    if remaining_hours > 0:
                        time_desc = f"{remaining_hours}小时{remaining_minutes}分钟"
                    elif remaining_minutes > 0:
                        time_desc = f"{remaining_minutes}分钟{remaining_seconds}秒"
                    else:
                        time_desc = f"{remaining_seconds}秒"

                    logger.warning(f"Token刷新通知在冷却期内，跳过发送: {notification_type} (还需等待 {time_desc})")
                    return
                self.pending_notification_keys.add(notification_key)

            if notification_type in ("slider_success", "slider_recovered_success"):
                slider_status_text = (
                    "账号会话已恢复"
                    if notification_type == "slider_recovered_success"
                    else "cookies已自动更新到数据库"
                )
                notification_msg = render_notification_template(
                    'slider_success',
                    account_id=current_account_id,
                    time=time.strftime('%Y-%m-%d %H:%M:%S'),
                    status_text=slider_status_text
                )
            elif "密码登录成功" in error_message or notification_type == "password_login_success":
                notification_msg = render_notification_template(
                    'password_login_success',
                    account_id=current_account_id,
                    time=time.strftime('%Y-%m-%d %H:%M:%S'),
                    cookie_count='已获取'
                )
            elif "刷新Cookie成功" in error_message or notification_type == "cookie_refresh_success":
                notification_msg = render_notification_template(
                    'cookie_refresh_success',
                    account_id=current_account_id,
                    time=time.strftime('%Y-%m-%d %H:%M:%S'),
                    cookie_count='已获取'
                )
            elif "人脸验证" in error_message or "短信验证" in error_message or "二维码验证" in error_message or "身份验证" in error_message or (verification_url and "passport" in verification_url):
                notification_msg = render_notification_template(
                    'face_verify',
                    account_id=current_account_id,
                    time=time.strftime('%Y-%m-%d %H:%M:%S'),
                    verification_url=verification_url or '',
                    verification_type=guess_verification_type(error_message, verification_url)
                )
            elif verification_url:
                notification_msg = render_notification_template(
                    'token_refresh',
                    account_id=current_account_id,
                    time=time.strftime('%Y-%m-%d %H:%M:%S'),
                    error_message=error_message,
                    verification_url=verification_url
                )
            else:
                notification_msg = render_notification_template(
                    'token_refresh',
                    account_id=current_account_id,
                    time=time.strftime('%Y-%m-%d %H:%M:%S'),
                    error_message=error_message,
                    verification_url='无'
                )

            logger.info(f"准备发送Token刷新异常通知: {self.account_id}")

            notification_sent = await dispatch_account_notifications(
                current_account_id,
                notification_msg,
                title='闲鱼管理系统通知',
                notification_type=notification_type,
                attachment_path=attachment_path,
            )

            if notification_sent:
                current_time = time.time()
                async with self.notification_lock:
                    self.last_notification_time[notification_key] = current_time

                if notification_type == "message_stream_stale":
                    next_send_time = current_time + self.message_stream_notification_cooldown
                    cooldown_desc = f"{max(1, int(self.message_stream_notification_cooldown // 60))}分钟"
                elif is_token_related_error:
                    next_send_time = current_time + self.token_refresh_notification_cooldown
                    cooldown_desc = "3小时"
                else:
                    next_send_time = current_time + self.notification_cooldown
                    cooldown_desc = f"{self.notification_cooldown // 60}分钟"

                next_send_time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_send_time))
                logger.info(f"Token刷新通知已发送，下次可发送时间: {next_send_time_str} (冷却时间: {cooldown_desc})")
            else:
                logger.warning(f"【{self.account_id}】Token刷新通知未发送成功，不进入冷却: {notification_type}")

        except Exception as e:
            logger.error(f"处理Token刷新通知失败: {self._safe_str(e)}")
        finally:
            async with self.notification_lock:
                self.pending_notification_keys.discard(f"token:{notification_type}")

    def _is_token_related_error(self, error_message: str) -> bool:
        normalized_message = self._safe_str(error_message).strip()
        if not normalized_message:
            return False

        message_lower = normalized_message.lower()

        success_markers = (
            "login success",
            "password_login_success",
            "cookie_refresh_success",
            "slider_success",
            "slider_recovered_success",
            "登录成功",
            "刷新cookie成功",
            "cookie已获取",
            "cookie已更新",
            "会话已恢复",
            "验证通过",
            "已恢复",
            "已获取",
            "已更新",
            "success",
        )
        if any(marker.lower() in message_lower for marker in success_markers):
            return False

        if self._is_normal_token_expiry(normalized_message):
            return True

        token_related_keywords = (
            "session过期",
            "session expired",
            "会话已失效",
            "会话失效",
            "页面会话已失效",
            "cookie验证失败",
            "cookie更新失败",
            "cookie失效",
            "cookie无效",
            "初始化时无法获取有效token",
            "token_init_failed",
            "token_refresh_failed",
            "token_refresh_exception",
            "captcha",
            "滑块",
            "人脸验证",
            "短信验证",
            "二维码验证",
            "验证url",
            "passport",
        )
        return any(keyword.lower() in message_lower for keyword in token_related_keywords)

    def _is_normal_token_expiry(self, error_message: str) -> bool:
        token_error_keywords = [
            'Token刷新失败',
            'Token刷新异常',
            'token刷新失败',
            'token刷新异常',
            'TOKEN刷新失败',
            'TOKEN刷新异常',
            'FAIL_SYS_USER_VALIDATE',
            'RGV587_ERROR',
            '哎哟喂,被挤爆啦',
            '请稍后重试',
            'punish?x5secdata',
            'captcha',
            '无法获取有效token',
            '无法获取有效Token',
            'Token获取失败',
            'token获取失败',
            'TOKEN获取失败',
            'Token定时刷新失败',
            'token定时刷新失败',
            'TOKEN定时刷新失败',
            '初始化时无法获取有效Token',
            '初始化时无法获取有效token',
            'accessToken',
            'access_token',
            '_m_h5_tk',
            'mtop.taobao.idlemessage.pc.login.token'
        ]

        error_message_lower = error_message.lower()
        for keyword in token_error_keywords:
            if keyword.lower() in error_message_lower:
                return True

        return False

    def _build_scheduled_token_refresh_error_message(self, last_refresh_status: str) -> str:
        if last_refresh_status in {"session_expired_after_slider", "session_expired_preflight"}:
            return "Session已过期，系统自动恢复失败，请重新登录"

        if last_refresh_status == "token_expired_recovery_failed":
            detail = (self.last_token_refresh_error_message or "").lower()
            if "session过期" in detail or "页面会话已失效" in detail:
                return "Session已过期，系统自动恢复失败，请重新登录"

        return "Token定时刷新失败，将自动重试"

    async def send_delivery_failure_notification(self, send_user_name: str, send_user_id: str, item_id: str, error_message: str, chat_id: str = None):
        try:
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error("【default】自动发货失败通知缺少 canonical account_id，拒绝继续运行")
                return
            notification_message = render_notification_template(
                'delivery',
                account_id=current_account_id,
                buyer_name=send_user_name,
                buyer_id=send_user_id,
                item_id=item_id,
                chat_id=chat_id or '未知',
                result=error_message,
                time=time.strftime('%Y-%m-%d %H:%M:%S')
            )

            notification_sent = await dispatch_account_notifications(
                current_account_id,
                notification_message,
                title='自动发货通知',
                notification_type='delivery',
            )
            if not notification_sent:
                logger.warning(f"【{self.account_id}】自动发货通知未发送成功")

        except Exception as e:
            logger.error(f"发自动发货知异常: {self._safe_str(e)}")

    async def auto_confirm(self, order_id, item_id=None, retry_count=0):
        try:
            logger.warning(f"【{self.account_id}】开始确认发货，订单ID: {order_id}")
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error(
                    f"【default】确认发货缺少 canonical account_id，拒绝继续运行 {order_id}"
                )
                return {"success": False, "error": "missing canonical account_id for auto_confirm", "order_id": order_id}

            from secure_confirm_decrypted import SecureConfirm

            secure_confirm = SecureConfirm(self.session, self.cookies_str, current_account_id, self)

            secure_confirm.current_token = self.current_token
            secure_confirm.last_token_refresh_time = self.last_token_refresh_time
            secure_confirm.token_refresh_interval = self.token_refresh_interval

            result = await secure_confirm.auto_confirm(order_id, item_id, retry_count)

            if secure_confirm.cookies_str != self.cookies_str:
                self._set_runtime_cookie_state(
                    cookies_str=secure_confirm.cookies_str,
                    cookies_dict=secure_confirm.cookies,
                    source="secure_confirm_sync",
                )
                logger.warning(f"【{self.account_id}】已同步确认发货模块更新的cookies")

            if secure_confirm.current_token != self.current_token:
                self.current_token = secure_confirm.current_token
                self.last_token_refresh_time = secure_confirm.last_token_refresh_time
                logger.warning(f"【{self.account_id}】已同步确认发货模块更新的token")

            return result

        except Exception as e:
            logger.error(f"【{self.account_id}】加密确认模块调用失败: {self._safe_str(e)}")
            return {"error": f"加密确认模块调用失败: {self._safe_str(e)}", "order_id": order_id}

    async def auto_freeshipping(self, order_id, item_id, buyer_id, retry_count=0):
        try:
            logger.warning(f"【{self.account_id}】开始免拼发货，订单ID: {order_id}")
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error(
                    f"【default】免拼发货缺少 canonical account_id，拒绝继续运行 {order_id}"
                )
                return {"success": False, "error": "missing canonical account_id for auto_freeshipping", "order_id": order_id}

            from secure_freeshipping_decrypted import SecureFreeshipping

            secure_freeshipping = SecureFreeshipping(self.session, self.cookies_str, current_account_id)

            secure_freeshipping.current_token = self.current_token
            secure_freeshipping.last_token_refresh_time = self.last_token_refresh_time
            secure_freeshipping.token_refresh_interval = self.token_refresh_interval

            result = await secure_freeshipping.auto_freeshipping(order_id, item_id, buyer_id, retry_count)

            if secure_freeshipping.cookies_str != self.cookies_str:
                self._set_runtime_cookie_state(
                    cookies_str=secure_freeshipping.cookies_str,
                    cookies_dict=secure_freeshipping.cookies,
                    source="secure_freeshipping_sync",
                )
                logger.warning(f"【{self.account_id}】已同步免拼发货模块更新的cookies")

            if secure_freeshipping.current_token != self.current_token:
                self.current_token = secure_freeshipping.current_token
                self.last_token_refresh_time = secure_freeshipping.last_token_refresh_time
                logger.warning(f"【{self.account_id}】已同步免拼发货模块更新的token")

            return result

        except Exception as e:
            logger.error(f"【{self.account_id}】免拼发货模块调用失败: {self._safe_str(e)}")
            return {"error": f"免拼发货模块调用失败: {self._safe_str(e)}", "order_id": order_id}

    async def fetch_recent_order_history_candidates(
        self,
        max_orders: int = 100,
        utc_start: str = None,
        utc_end_exclusive: str = None,
    ):
        current_account_id = self._canonical_account_id()
        if not current_account_id:
            logger.error("【default】历史订单列表抓取缺少 canonical account_id，拒绝继续运行")
            return {
                "orders": [],
                "scanned_count": 0,
                "matched_count": 0,
                "out_of_range_count": 0,
                "pages_scanned": 0,
                "stopped_by_range": False,
            }

        history_fetcher = None
        try:
            from utils.order_history_sync import OrderHistoryPageFetcher

            history_fetcher = OrderHistoryPageFetcher(
                self.cookies_str,
                account_id=current_account_id,
                headless=True,
            )
            fetch_result = await history_fetcher.fetch_recent_orders(
                max_orders=max_orders,
                utc_start=utc_start,
                utc_end_exclusive=utc_end_exclusive,
            )

            if history_fetcher.cookie_string and history_fetcher.cookie_string != self.cookies_str:
                self._set_runtime_cookie_state(
                    cookies_str=history_fetcher.cookie_string,
                    cookies_dict=history_fetcher.cookies,
                    source="order_history_sync_http",
                )

            return fetch_result
        finally:
            if history_fetcher is not None:
                try:
                    await history_fetcher.close()
                except Exception as close_error:
                    logger.warning(f"【{current_account_id}】关闭历史订单抓取器失败: {self._safe_str(close_error)}")

    async def fetch_order_detail_info(self, order_id: str, item_id: str = None, buyer_id: str = None, debug_headless: bool = None, sid: str = None, force_refresh: bool = False, buyer_nick: str = None, buyer_id_source: str = None):
        current_account_id = self._canonical_account_id()
        if not current_account_id:
            logger.error(
                f"【default】订单详情抓取缺少 canonical account_id，拒绝继续运行: {order_id}"
            )
            return None

        order_detail_scope_key = self._compose_order_detail_scope_key(current_account_id, order_id)
        if not order_detail_scope_key:
            logger.error(
                f"【{current_account_id}】订单详情抓取缺少有效作用域键，拒绝继续运行: order_id={order_id}"
            )
            return None

        order_detail_lock = self._order_detail_locks.get(order_detail_scope_key)
        if order_detail_lock is None:
            order_detail_lock = asyncio.Lock()
            self._order_detail_locks[order_detail_scope_key] = order_detail_lock

        try:
            current_loop = asyncio.get_running_loop()
            lock_loop = getattr(order_detail_lock, '_loop', None)
            if lock_loop is not None and lock_loop is not current_loop:
                order_detail_lock = asyncio.Lock()
                self._order_detail_locks[order_detail_scope_key] = order_detail_lock
                logger.info(f"【{current_account_id}】订单详情锁 {order_id} 事件循环不匹配，已重建")
        except RuntimeError:
            pass

        self._order_detail_lock_times[order_detail_scope_key] = time.time()

        async with order_detail_lock:
            logger.info(f"🔍 【{current_account_id}】获取订单详情锁 {order_id}，开始处理...")

            try:
                logger.info(f"【{self.account_id}】开始获取订单详情: {order_id}, sid={sid}")

                from utils.order_detail_fetcher import fetch_order_detail_simple
                from db_manager import db_manager

                cookie_string = self.cookies_str
                logger.warning(f"【{self.account_id}】使用Cookie长度: {len(cookie_string) if cookie_string else 0}")

                headless_mode = True if debug_headless is None else debug_headless
                if not headless_mode:
                    logger.info(f"【{self.account_id}】🖥️ 启用有头模式进行调试")

                result = await fetch_order_detail_simple(
                    order_id,
                    cookie_string,
                    headless=headless_mode,
                    force_refresh=force_refresh,
                    account_id=current_account_id,
                )

                if result:
                    retry_task = self.order_detail_retry_tasks.get(order_detail_scope_key)
                    current_task = asyncio.current_task()
                    if retry_task and retry_task is not current_task and not retry_task.done():
                        retry_task.cancel()
                        self.order_detail_retry_tasks.pop(order_detail_scope_key, None)
                        logger.info(f"【{current_account_id}】订单详情已成功获取，取消待执行的补抓任务: {order_id}")

                    logger.info(f"【{self.account_id}】订单详情获取成功: {order_id}")
                    logger.info(f"【{self.account_id}】页面标题: {result.get('title', '未知')}")

                    def _normalize_optional_text(value):
                        if value is None:
                            return None
                        text = str(value).strip()
                        return text if text else None

                    def _normalize_amount_text(value):
                        text = _normalize_optional_text(value)
                        if not text:
                            return None
                        if not re.search(r'\d', text):
                            return None
                        return text

                    def _parse_amount_float(value):
                        text = _normalize_amount_text(value)
                        if not text:
                            return None
                        try:
                            return float(text)
                        except (TypeError, ValueError):
                            return None

                    spec_parse_mode = str(result.get('spec_parse_mode') or '').strip() or 'no_spec'
                    spec_name = _normalize_optional_text(result.get('spec_name'))
                    spec_value = _normalize_optional_text(result.get('spec_value'))
                    spec_name_2 = _normalize_optional_text(result.get('spec_name_2'))
                    spec_value_2 = _normalize_optional_text(result.get('spec_value_2'))
                    quantity = _normalize_optional_text(result.get('quantity'))
                    amount = _normalize_amount_text(result.get('amount'))
                    amount_source = _normalize_optional_text(result.get('amount_source')) or 'unknown'
                    platform_created_at = _normalize_optional_text(result.get('platform_created_at'))
                    platform_paid_at = _normalize_optional_text(result.get('platform_paid_at'))
                    platform_completed_at = _normalize_optional_text(result.get('platform_completed_at'))
                    item_config = db_manager.get_item_info(current_account_id, item_id) if item_id else None
                    item_config_multi_spec = bool(item_config and item_config.get('is_multi_spec'))
                    item_config_detail = _normalize_optional_text(item_config.get('item_detail')) if item_config else None
                    is_coin_deduction_item = bool(item_config_detail and '闲鱼币抵扣' in item_config_detail)
                    configured_item_amount = _normalize_amount_text(item_config.get('item_price')) if item_config else None
                    configured_item_amount_value = _parse_amount_float(configured_item_amount)

                    if item_config is not None and not item_config_multi_spec and any(
                        [spec_name, spec_value, spec_name_2, spec_value_2]
                    ):
                        logger.warning(
                            f"【{self.account_id}】商品配置为无规格，刷新订单详情时忽略解析到的规格信息: "
                            f"order_id={order_id}, item_id={item_id}, "
                            f"spec={spec_name or ''}:{spec_value or ''}, spec2={spec_name_2 or ''}:{spec_value_2 or ''}"
                        )
                        spec_name = None
                        spec_value = None
                        spec_name_2 = None
                        spec_value_2 = None

                    if spec_parse_mode == 'one_spec' and spec_name and spec_value and not (spec_name_2 or spec_value_2):
                        spec_name_2 = ''
                        spec_value_2 = ''
                        logger.info(
                            f"【{self.account_id}】订单详情明确解析为单规格，允许清空历史残留的第二规格字段: "
                            f"order_id={order_id}, item_id={item_id}, spec={spec_name}:{spec_value}"
                        )

                    raw_order_status = _normalize_optional_text(result.get('order_status'))
                    order_status_source = _normalize_optional_text(result.get('order_status_source')) or 'unknown'
                    order_status = raw_order_status if raw_order_status and raw_order_status.lower() != 'unknown' else None
                    if order_status:
                        logger.info(f"【{self.account_id}】📊 订单状态: {order_status} (source={order_status_source})")
                    elif raw_order_status and raw_order_status.lower() == 'unknown':
                        logger.warning(f"【{self.account_id}】订单状态解析为unknown，跳过状态字段写库")

                    if spec_name and spec_value:
                        logger.info(f"【{self.account_id}】📋 规格名称: {spec_name}")
                        logger.info(f"【{self.account_id}】📝 规格值: {spec_value}")
                        if spec_name_2 and spec_value_2:
                            logger.info(f"【{self.account_id}】📋 规格2名称: {spec_name_2}")
                            logger.info(f"【{self.account_id}】📝 规格2值: {spec_value_2}")
                            print(f"🛍️ 【{self.account_id}】订单 {order_id} 规格信息: {spec_name} -> {spec_value}, {spec_name_2} -> {spec_value_2}")
                        else:
                            print(f"🛍️ 【{self.account_id}】订单 {order_id} 规格信息: {spec_name} -> {spec_value}")
                    else:
                        logger.warning(f"【{self.account_id}】未获取到有效的规格信息")
                        print(f"⚠️ 【{self.account_id}】订单 {order_id} 规格信息获取失败")

                    if amount:
                        logger.info(f"【{self.account_id}】💰 订单金额: {amount} (source={amount_source})")

                    try:
                        existing_order = db_manager.get_order_by_id(order_id, account_id=current_account_id)
                        current_order_status = existing_order.get('order_status') if existing_order else None
                        existing_amount = existing_order.get('amount') if existing_order else None
                        existing_amount_value = _parse_amount_float(existing_amount)
                        amount, amount_source = self._apply_bargain_amount_override(
                            order_id,
                            item_id,
                            amount,
                            amount_source,
                            existing_order=existing_order,
                            item_config=item_config,
                        )
                        incoming_amount_value = _parse_amount_float(amount)
                        has_valid_spec = bool(spec_name and spec_value)
                        low_confidence_amount_sources = {
                            'selector_direct',
                            'selector_currency',
                            'text_currency',
                            'unknown',
                        }

                        if (
                            is_coin_deduction_item and existing_amount_value is not None and incoming_amount_value is not None and
                            configured_item_amount_value is not None and existing_amount_value + 0.009 < configured_item_amount_value and
                            abs(incoming_amount_value - configured_item_amount_value) <= 0.009
                        ):
                            logger.warning(
                                f"【{self.account_id}】闲鱼币抵扣订单返回原价，保留已有实付金额: "
                                f"order_id={order_id}, existing_amount={existing_amount}, incoming_amount={amount}, "
                                f"configured_amount={configured_item_amount}, amount_source={amount_source}"
                            )
                            amount = _normalize_amount_text(existing_amount)
                            amount_source = 'coin_deduction_preserved_existing'
                            incoming_amount_value = _parse_amount_float(amount)

                        if amount and amount_source in low_confidence_amount_sources and not has_valid_spec and not order_status:
                            if existing_amount_value is not None:
                                logger.warning(
                                    f"【{self.account_id}】订单详情返回低置信度金额，保留已有金额: "
                                    f"order_id={order_id}, existing_amount={existing_amount}, incoming_amount={amount}, "
                                    f"amount_source={amount_source}"
                                )
                                amount = _normalize_amount_text(existing_amount)
                                amount_source = 'preserved_existing'
                            else:
                                logger.warning(
                                    f"【{self.account_id}】订单详情返回低置信度金额，且缺少规格/状态佐证，跳过写库: "
                                    f"order_id={order_id}, incoming_amount={amount}, amount_source={amount_source}"
                                )
                                amount = None

                        elif (
                            amount and existing_amount_value is not None and incoming_amount_value is not None and
                            abs(existing_amount_value - incoming_amount_value) > 0.009 and
                            not has_valid_spec and not order_status and
                            amount_source not in {'selector_keyword_high', 'selector_keyword_low', 'text_keyword_high', 'text_keyword_low', 'cache'}
                        ):
                            logger.warning(
                                f"【{self.account_id}】订单详情金额跳变且缺少规格/状佐证，保留已有金额: "
                                f"order_id={order_id}, existing_amount={existing_amount}, incoming_amount={amount}, "
                                f"amount_source={amount_source}"
                            )
                            amount = _normalize_amount_text(existing_amount)
                            amount_source = 'preserved_existing'

                        if self._should_reject_order_detail_status_update(
                            current_status=current_order_status,
                            incoming_status=order_status,
                            incoming_source=order_status_source,
                            force_refresh=force_refresh,
                        ):
                            logger.warning(
                                f"【{self.account_id}】强制刷新结果仅来自正文，拒绝将订单状更新为completed: "
                                f"order_id={order_id}, current={current_order_status}, incoming={order_status}, "
                                f"source={order_status_source}"
                            )
                            order_status = None

                        normalized_current_order_status = db_manager._normalize_order_status(current_order_status)
                        normalized_incoming_order_status = db_manager._normalize_order_status(order_status)
                        if self._should_accept_order_detail_status_correction(
                            current_order_status,
                            order_status,
                            order_status_source,
                            force_refresh=force_refresh,
                            order_id=order_id,
                        ):
                            order_status_to_save = normalized_incoming_order_status
                            logger.warning(
                                f"【{self.account_id}】检测到可疑已发货状态，允许强刷后的结构化待发货结果纠偏: "
                                f"order_id={order_id}, current={current_order_status}, incoming={order_status}, "
                                f"source={order_status_source}"
                            )
                        else:
                            order_status_to_save = self._resolve_external_order_status(
                                current_order_status,
                                order_status,
                                source='order_detail_refresh'
                            )

                        if (
                            order_status and existing_order and order_status_to_save is None and
                            normalized_current_order_status != normalized_incoming_order_status
                        ):
                            logger.info(
                                f"【{self.account_id}】保留订单现有状态，跳过详情页覆盖: "
                                f"order_id={order_id}, current={current_order_status}, incoming={order_status}"
                            )

                        buyer_id_to_save, buyer_nick_to_save, should_skip_write = self._select_buyer_identity_for_order_write(
                            order_id,
                            incoming_buyer_id=buyer_id,
                            incoming_buyer_nick=buyer_nick,
                            existing_order=existing_order,
                            buyer_id_source=buyer_id_source,
                            buyer_nick_source="order_detail",
                            log_prefix=f"【{self.account_id}?",
                        )
                        if should_skip_write:
                            return result

                        cookie_info = db_manager.get_cookie_by_id(current_account_id)
                        if not cookie_info:
                            logger.warning(f"账号ID {current_account_id} 不存在于cookies表中，丢弃订单 {order_id}")
                        else:
                            success = db_manager.insert_or_update_order(
                                order_id=order_id,
                                item_id=item_id,
                                buyer_id=buyer_id_to_save,
                                buyer_nick=buyer_nick_to_save,
                                sid=sid,
                                spec_name=spec_name,
                                spec_value=spec_value,
                                spec_name_2=spec_name_2,
                                spec_value_2=spec_value_2,
                                quantity=quantity,
                                amount=amount,
                                account_id=current_account_id,
                                order_status=order_status_to_save,
                                platform_created_at=platform_created_at,
                                platform_paid_at=platform_paid_at,
                                platform_completed_at=platform_completed_at
                            )

                            logger.info(f"【{self.account_id}】检查订单状态处理器调用条件: success={success}, handler_exists={self.order_status_handler is not None}")
                            if success and self.order_status_handler:
                                logger.info(f"【{self.account_id}】准备调用订单状态处理器.handle_order_detail_fetched_status: {order_id}")
                                try:
                                    handler_result = self.order_status_handler.handle_order_detail_fetched_status(
                                        order_id=order_id,
                                        account_id=current_account_id,
                                        context="订单详情已拉取"
                                    )
                                    logger.info(f"【{self.account_id}】订单状态处理器.handle_order_detail_fetched_status返回结果: {handler_result}")

                                    logger.info(f"【{self.account_id}】准备调用订单状态处理器.on_order_details_fetched: {order_id}")
                                    self.order_status_handler.on_order_details_fetched(
                                        order_id,
                                        account_id=current_account_id,
                                    )
                                    logger.info(f"【{self.account_id}】订单状态处理器.on_order_details_fetched调用成功: {order_id}")
                                except Exception as e:
                                    logger.error(f"【{self.account_id}】订单状态处理器调用失败: {self._safe_str(e)}")
                                    import traceback
                                    logger.error(f"【{self.account_id}】详细错误信息: {traceback.format_exc()}")
                            else:
                                logger.warning(f"【{self.account_id}】订单状态处理器调用条件不满足: success={success}, handler_exists={self.order_status_handler is not None}")

                            if success:
                                logger.info(f"【{self.account_id}】订单信息已保存到数据库: {order_id}")
                                print(f"💾 【{self.account_id}】订单 {order_id} 信息已保存到数据库")
                            else:
                                logger.warning(f"【{self.account_id}】订单信息保存失败: {order_id}")

                    except Exception as db_e:
                        logger.error(f"【{self.account_id}】保存订单信息到数据库失败: {self._safe_str(db_e)}")

                    return result
                else:
                    logger.warning(f"【{self.account_id}】订单详情获取失败: {order_id}")
                    return None

            except Exception as e:
                logger.error(f"【{self.account_id}】获取订单详情异常: {self._safe_str(e)}")
                return None

    async def _auto_delivery(self, item_id: str, item_title: str = None, order_id: str = None, send_user_id: str = None,
                             chat_id: str = None, send_user_name: str = None, include_meta: bool = False,
                             data_preview_index: int = 0, delivery_unit_index: int = 1):
        try:
            matched_rule_context = None
            match_mode_context = None
            current_account_id = self._canonical_account_id()

            def build_result(success: bool, content: str = None, error: str = None, matched_rule: dict = None,
                             match_mode_value: str = None, delivery_steps_value: list = None):
                order_spec_mode_value = 'no_spec'
                item_config_mode_value = 'no_spec'
                rule_spec_mode_value = None

                try:
                    order_spec_mode_value = _get_order_spec_mode()
                except Exception:
                    pass

                try:
                    rule_spec_mode_value = _get_rule_spec_mode(matched_rule) if matched_rule else None
                except Exception:
                    pass

                try:
                    item_config_mode_value = 'spec_enabled' if item_config_multi_spec else 'no_spec'
                except Exception:
                    pass

                if include_meta:
                    return {
                        "success": bool(success),
                        "content": content if success else None,
                        "error": error if not success else None,
                        "rule_id": matched_rule.get('id') if matched_rule else None,
                        "rule_keyword": matched_rule.get('keyword') if matched_rule else None,
                        "card_type": matched_rule.get('card_type') if matched_rule else None,
                        "match_mode": match_mode_value,
                        "order_spec_mode": order_spec_mode_value,
                        "rule_spec_mode": rule_spec_mode_value,
                        "item_config_mode": item_config_mode_value,
                        "card_id": matched_rule.get('card_id') if matched_rule else None,
                        "card_description": matched_rule.get('card_description') if matched_rule else None,
                        "delivery_steps": delivery_steps_value or [],
                        "data_card_pending_consume": False,
                        "data_line": None,
                        "data_reservation_id": None,
                        "data_reservation_status": None,
                        "delivery_unit_index": delivery_unit_index
                    }
                return content if success else None

            from db_manager import db_manager

            if not current_account_id:
                logger.error(
                    "【default】自动发货缺少 canonical account_id，拒绝继续运行"
                )
                return build_result(False, error="自动发货缺少 canonical account_id，无法继续")

            logger.info(f"开始自动发货检查: 商品ID={item_id}")

            item_info = None
            search_text = item_title
            if item_id and item_id != "未知商品":
                try:
                    logger.info(f"从数据库获取商品信息: {item_id}")
                    db_item_info = db_manager.get_item_info(current_account_id, item_id)
                    if db_item_info:
                        item_info = db_item_info
                        item_title_db = db_item_info.get('item_title', '') or ''
                        item_detail_db = db_item_info.get('item_detail', '') or ''

                        if not item_detail_db.strip():
                            from config import config
                            auto_fetch_config = config.get('ITEM_DETAIL', {}).get('auto_fetch', {})

                            if auto_fetch_config.get('enabled', True):
                                logger.info(f"数据库中商品详情为空，尝试自动获取: {item_id}")
                                try:
                                    fetched_detail = await self.fetch_item_detail_from_api(item_id)
                                    if fetched_detail:
                                        await self.save_item_detail_only(item_id, fetched_detail)
                                        item_detail_db = fetched_detail
                                        logger.info(f"成功获取并保存商品详情: {item_id}")
                                    else:
                                        logger.warning(f"未能获取到商品详情: {item_id}")
                                except Exception as api_e:
                                    logger.warning(f"获取商品详情失败: {item_id}, 错误: {self._safe_str(api_e)}")
                            else:
                                logger.warning(f"自动获取商品详情功能已禁用，跳过: {item_id}")

                        search_parts = []
                        if item_title_db.strip():
                            search_parts.append(item_title_db.strip())
                        if item_detail_db.strip():
                            search_parts.append(item_detail_db.strip())

                        if search_parts:
                            search_text = ' '.join(search_parts)
                            logger.info(f"使用数据库商品标题+详情作为搜索文本: 标题='{item_title_db}', 详情长度={len(item_detail_db)}")
                            logger.warning(f"完整搜索文本: {search_text[:200]}...")
                        else:
                            logger.warning(f"数据库中商品标题和详情都为空: {item_id}")
                            search_text = item_title or item_id
                    else:
                        logger.warning(f"数据库中未找到商品信息: {item_id}")
                        search_text = item_title or item_id

                except Exception as db_e:
                    logger.warning(f"从数据库获取商品信息失败: {self._safe_str(db_e)}")
                    search_text = item_title or item_id

            if not search_text:
                search_text = item_id or "未知商品"

            logger.info(f"使用搜索文本匹配发货规则: {search_text[:100]}...")

            item_config_multi_spec = db_manager.get_item_multi_spec_status(current_account_id, item_id)
            spec_name = ''
            spec_value = ''
            spec_name_2 = ''
            spec_value_2 = ''

            def _apply_spec_from_order_detail(order_detail_data) -> bool:
                nonlocal spec_name, spec_value, spec_name_2, spec_value_2
                if not order_detail_data or not isinstance(order_detail_data, dict):
                    return False
                spec_name = (order_detail_data.get('spec_name') or '').strip()
                spec_value = (order_detail_data.get('spec_value') or '').strip()
                spec_name_2 = (order_detail_data.get('spec_name_2') or '').strip()
                spec_value_2 = (order_detail_data.get('spec_value_2') or '').strip()
                return bool(spec_name and spec_value)

            def _get_order_spec_mode() -> str:
                has_first_spec = bool(spec_name and spec_value)
                has_second_spec = bool(spec_name_2 and spec_value_2)

                if has_first_spec and has_second_spec:
                    return 'two_spec'
                if has_first_spec:
                    return 'one_spec'
                return 'no_spec'

            def _get_rule_spec_mode(rule: dict) -> str:
                if not rule:
                    return 'no_spec'

                rule_spec_name = (rule.get('spec_name') or '').strip()
                rule_spec_value = (rule.get('spec_value') or '').strip()
                rule_spec_name_2 = (rule.get('spec_name_2') or '').strip()
                rule_spec_value_2 = (rule.get('spec_value_2') or '').strip()

                if rule_spec_name and rule_spec_value and rule_spec_name_2 and rule_spec_value_2:
                    return 'two_spec'
                if rule_spec_name and rule_spec_value:
                    return 'one_spec'
                return 'no_spec'

            if order_id:
                logger.info(f"检测到订单ID，获取订单详情用于规则匹配: {order_id}")
                max_detail_attempts = 3 if item_config_multi_spec else 1
                for attempt in range(1, max_detail_attempts + 1):
                    try:
                        force_refresh = attempt > 1
                        if force_refresh:
                            logger.info(f"订单规格信息缺失，开始强刷重试 ({attempt}/{max_detail_attempts}): {order_id}")

                        order_detail = await self.fetch_order_detail_info(
                            order_id,
                            item_id,
                            send_user_id,
                            force_refresh=force_refresh
                        )

                        if _apply_spec_from_order_detail(order_detail):
                            logger.info(f"获取到规格信息: {spec_name} = {spec_value}")
                            if spec_name_2 and spec_value_2:
                                logger.info(f"获取到规格2信息: {spec_name_2} = {spec_value_2}")
                            break

                        if item_config_multi_spec:
                            logger.warning(
                                f"订单详情已获取但未解析到有效规格信息 (尝试 {attempt}/{max_detail_attempts})"
                            )
                        else:
                            logger.info("无规格商品未解析到规格信息，按普通规则继续")
                    except Exception as e:
                        logger.error(
                            f"获取订单详情失败 (尝试 {attempt}/{max_detail_attempts}): {self._safe_str(e)}"
                        )

                    if attempt < max_detail_attempts:
                        await asyncio.sleep(0.6)

                if _get_order_spec_mode() == 'no_spec':
                    try:
                        cached_order = db_manager.get_order_by_id(order_id, account_id=current_account_id)
                        if cached_order and _apply_spec_from_order_detail(cached_order):
                            logger.warning(
                                f"订单 {order_id} 从数据库缓存恢复规格成功: "
                                f"{spec_name}:{spec_value}"
                            )
                    except Exception as cache_e:
                        logger.warning(f"订单缓存规格恢复失败: {self._safe_str(cache_e)}")
            else:
                logger.warning("当前无订单ID，跳过订单详情拉取，将仅基于商品文本匹配规则")

            order_spec_mode = _get_order_spec_mode()
            item_config_mode = 'spec_enabled' if item_config_multi_spec else 'no_spec'

            if order_spec_mode != 'no_spec' and item_info is not None and not item_config_multi_spec:
                logger.warning(
                    f"商品已配置为无规格，忽略订单解析到的规格并按普通规则匹配: "
                    f"order_spec_mode={order_spec_mode}, item_id={item_id or 'unknown'}, "
                    f"order_id={order_id or 'unknown'}, spec={spec_name}:{spec_value}"
                )
                spec_name = ''
                spec_value = ''
                spec_name_2 = ''
                spec_value_2 = ''
                order_spec_mode = _get_order_spec_mode()
            elif order_spec_mode == 'no_spec' and item_config_multi_spec:
                block_reason = (
                    f"商品已开启规格匹配，但订单未解析到有效规格信息，已阻断自动发货: "
                    f"order_id={order_id or 'unknown'}, item_id={item_id or 'unknown'}"
                )
                logger.error(block_reason)
                return build_result(False, error=block_reason, match_mode_value='blocked_no_spec_parsed')

            logger.info(
                f"规格模式判定完成: order_spec_mode={order_spec_mode}, "
                f"item_config_mode={item_config_mode}"
            )

            delivery_rules = []
            if order_spec_mode == 'two_spec':
                match_mode = 'two_spec_exact'
                match_mode_context = match_mode
                logger.info(
                    f"尝试精确匹配两组规格发货规则: {search_text[:50]}... "
                    f"[{spec_name}:{spec_value}, {spec_name_2}:{spec_value_2}]"
                )
                delivery_rules = db_manager.get_delivery_rules_by_keyword_and_spec(
                    search_text,
                    spec_name,
                    spec_value,
                    spec_name_2,
                    spec_value_2,
                    user_id=self.user_id,
                    expected_mode='two_spec'
                )
                if not delivery_rules:
                    error_message = "两组规格订单未找到匹配的发货规则"
                    logger.warning(f"{error_message}: {search_text[:50]}...")
                    return build_result(False, error=error_message, match_mode_value='blocked_no_rule')
            elif order_spec_mode == 'one_spec':
                match_mode = 'one_spec_exact'
                match_mode_context = match_mode
                logger.info(
                    f"尝试精确匹配两组规格发货规则: {search_text[:50]}... "
                    f"[{spec_name}:{spec_value}]"
                )
                delivery_rules = db_manager.get_delivery_rules_by_keyword_and_spec(
                    search_text,
                    spec_name,
                    spec_value,
                    spec_name_2,
                    spec_value_2,
                    user_id=self.user_id,
                    expected_mode='one_spec'
                )
                if not delivery_rules:
                    logger.warning(
                        f"一组规格订单未找到精确规格规则，尝试降级匹配普通发货规则: {search_text[:50]}..."
                    )
                    fallback_rules = db_manager.get_delivery_rules_by_keyword(
                        search_text,
                        user_id=self.user_id,
                        only_non_multi_spec=True
                    )
                    if not fallback_rules:
                        error_message = "一组规格订单未找到匹配的发货规则"
                        logger.warning(f"{error_message}: {search_text[:50]}...")
                        return build_result(False, error=error_message, match_mode_value='blocked_no_rule')
                    if len(fallback_rules) != 1:
                        block_reason = (
                            f"丢组规格订单精确匹配失败后，普通规则兜底匹配到{len(fallback_rules)}条，"
                            f"已阻断自动发货以避免错发: order_id={order_id or 'unknown'}, "
                            f"item_id={item_id or 'unknown'}"
                        )
                        logger.error(block_reason)
                        return build_result(False, error=block_reason, match_mode_value='blocked_multiple_no_spec_rules')
                    delivery_rules = fallback_rules
                    match_mode = 'one_spec_fallback_no_spec'
                    match_mode_context = match_mode
                    logger.warning(
                        f"一组规格订单已降级命中唯一普通规则: order_id={order_id or 'unknown'}, "
                        f"item_id={item_id or 'unknown'}, rule_id={delivery_rules[0].get('id')}"
                    )
            else:
                match_mode = 'no_spec_match'
                match_mode_context = match_mode
                logger.info(f"无规格订单，尝试匹配普通发货规则: {search_text[:50]}...")
                delivery_rules = db_manager.get_delivery_rules_by_keyword(
                    search_text,
                    user_id=self.user_id,
                    only_non_multi_spec=True
                )
                if not delivery_rules:
                    error_message = "无规格订单未找到匹配的普通发货规则"
                    logger.warning(f"{error_message}: {search_text[:50]}...")
                    return build_result(False, error=error_message, match_mode_value='blocked_no_rule')
                if len(delivery_rules) != 1:
                    block_reason = (
                        f"无规格订单匹配到{len(delivery_rules)}条普通规则，已阻断自动发货以避免错发: "
                        f"order_id={order_id or 'unknown'}, item_id={item_id or 'unknown'}"
                    )
                    logger.error(block_reason)
                    return build_result(False, error=block_reason, match_mode_value='blocked_multiple_no_spec_rules')

            rule = delivery_rules[0]
            matched_rule_context = rule
            rule_spec_mode = _get_rule_spec_mode(rule)

            logger.info(
                f"规则模式判定完成: order_spec_mode={order_spec_mode}, rule_spec_mode={rule_spec_mode}, "
                f"match_mode={match_mode}, rule_id={rule.get('id')}"
            )

            allow_one_spec_fallback = (
                match_mode == 'one_spec_fallback_no_spec'
                and order_spec_mode == 'one_spec'
                and rule_spec_mode == 'no_spec'
            )

            if rule_spec_mode != order_spec_mode and not allow_one_spec_fallback:
                block_reason = (
                    f"订单规格模式与命中规则模式不一致，已阻断自动发货: "
                    f"order_spec_mode={order_spec_mode}, rule_spec_mode={rule_spec_mode}, "
                    f"order_id={order_id or 'unknown'}, item_id={item_id or 'unknown'}, rule_id={rule.get('id')}"
                )
                logger.error(block_reason)
                return build_result(False, error=block_reason, matched_rule=rule, match_mode_value='blocked_rule_mode_mismatch')

            item_title_for_save = None
            try:
                db_item_info = db_manager.get_item_info(self.account_id, item_id)
                if db_item_info:
                    item_title_for_save = db_item_info.get('item_title', '').strip()
            except Exception:
                pass
            await self.save_item_info_to_db(item_id, search_text, item_title_for_save)
            logger.warning(f"跳过保存商品信息：缺少商品标题 - {item_id}")

            if order_spec_mode == 'two_spec':
                rule_spec_info = f"{rule['spec_name']}:{rule['spec_value']}, {rule['spec_name_2']}:{rule['spec_value_2']}"
                order_spec_info = f"{spec_name}:{spec_value}, {spec_name_2}:{spec_value_2}"
                logger.info(f"🎯 精确匹配两组规格发货规则: {rule['keyword']} -> {rule['card_name']} [{rule_spec_info}]")
                logger.info(f"📋 订单规格: {order_spec_info} ✅ 匹配卡券规格: {rule_spec_info}")
            elif match_mode == 'one_spec_fallback_no_spec':
                order_spec_info = f"{spec_name}:{spec_value}"
                logger.warning(
                    f"⚠️ 单规格订单降级匹配普通发货规则: {rule['keyword']} -> {rule['card_name']} ({rule['card_type']})"
                )
                logger.warning(f"📋 订单规格: {order_spec_info}，精确规格未命中，已降级到普通规则")
            elif order_spec_mode == 'one_spec':
                rule_spec_info = f"{rule['spec_name']}:{rule['spec_value']}"
                order_spec_info = f"{spec_name}:{spec_value}"
                logger.info(f"🎯 精确匹配两组规格发货规则: {rule['keyword']} -> {rule['card_name']} [{rule_spec_info}]")
                logger.info(f"📋 订单规格: {order_spec_info} ✅ 匹配卡券规格: {rule_spec_info}")
            else:
                logger.info(f"✅ 匹配无规格发货规则: {rule['keyword']} -> {rule['card_name']} ({rule['card_type']})")

            delay_seconds = rule.get('card_delay_seconds', 0)

            if delay_seconds and delay_seconds > 0:
                logger.info(f"检测到发货延时设置: {delay_seconds}秒，开始延时...")
                await asyncio.sleep(delay_seconds)
                logger.info(f"延时完成")

            if order_id:
                try:
                    from db_manager import db_manager

                    if send_user_id and send_user_id == self.myid:
                        logger.info(f"【{self.account_id}】跳过买家订单 {order_id}，buyer_id={send_user_id} 等于自己的ID")
                    else:
                        cookie_info = db_manager.get_cookie_by_id(current_account_id)
                        if not cookie_info:
                            logger.warning(f"账号ID {current_account_id} 不存在于cookies表中，丢弃订单 {order_id}")
                        else:
                            existing_order = db_manager.get_order_by_id(order_id, account_id=current_account_id)
                            if not existing_order:
                                logger.warning(
                                    f"【{current_account_id}】订单 {order_id} 未验证归属，跳过自动发货前的基础订单首写"
                                )
                except Exception as db_e:
                    logger.error(f"保存基本订单信息失败: {self._safe_str(db_e)}")

                logger.info(f"开始处理发货内容，规则: {rule['keyword']} -> {rule['card_name']} ({rule['card_type']})")

                delivery_content = None
                data_line = None
                data_reservation = None

                if rule['card_type'] == 'api':
                    delivery_content = await self._get_api_card_content(rule, order_id, item_id, send_user_id, spec_name, spec_value)

                elif rule['card_type'] == 'yifan_api':
                    delivery_content = await self._get_yifan_api_card_content(rule, order_id, item_id, send_user_id, chat_id)

                elif rule['card_type'] == 'text':
                    delivery_content = rule['text_content']

                elif rule['card_type'] == 'data':
                    data_reservation = db_manager.reserve_batch_data(
                        card_id=rule['card_id'],
                        order_id=order_id,
                        unit_index=delivery_unit_index,
                        account_id=current_account_id,
                        buyer_id=send_user_id,
                    )
                    if data_reservation:
                        data_line = data_reservation.get('reserved_content')
                        delivery_content = data_line
                    else:
                        delivery_content = None

                elif rule['card_type'] == 'image':
                    image_url = rule.get('image_url')
                    if image_url:
                        delivery_content = f"__IMAGE_SEND__{rule['card_id']}|{image_url}"
                        logger.info(f"准备发送图片: {image_url} (卡券ID: {rule['card_id']})")
                    else:
                        logger.error(f"图片卡券缺少图片URL: 卡券ID={rule['card_id']}")
                        delivery_content = None

                if delivery_content:
                    delivery_steps = self._build_delivery_steps(delivery_content, rule.get('card_description', ''))
                    if not delivery_steps:
                        logger.warning(f"发货步骤构建失败: 规则ID={rule['id']}")
                        return build_result(False, error=f"发货步骤构建失败: 规则ID={rule['id']}", matched_rule=rule, match_mode_value=match_mode)

                    if len(delivery_steps) == 1 and delivery_steps[0].get('type') == 'text':
                        final_content = delivery_steps[0].get('content') or ''
                    else:
                        final_content = delivery_content

                    logger.info(f"自动发货内容准备成功: 规则ID={rule['id']}, 步骤数={len(delivery_steps)}")

                    result = build_result(
                        True,
                        content=final_content,
                        matched_rule=rule,
                        match_mode_value=match_mode,
                        delivery_steps_value=delivery_steps
                    )
                    if include_meta and isinstance(result, dict):
                        result['card_id'] = rule.get('card_id')
                        result['data_card_pending_consume'] = bool(rule['card_type'] == 'data')
                        result['data_line'] = data_line
                        result['data_reservation_id'] = data_reservation.get('id') if data_reservation else None
                        result['data_reservation_status'] = data_reservation.get('status') if data_reservation else None
                        result['delivery_unit_index'] = delivery_unit_index
                    return result
                else:
                    logger.warning(f"获取发货内容失败: 规则ID={rule['id']}")
                    return build_result(False, error=f"获取发货内容失败: 规则ID={rule['id']}", matched_rule=rule, match_mode_value=match_mode)
            else:
                logger.info(f"⚠️ 未检测到订单ID，跳过发货内容处理。规则: {rule['keyword']} -> {rule['card_name']} ({rule['card_type']})")
                return build_result(False, error="未检测到订单ID，跳过发货内容处理", matched_rule=rule, match_mode_value=match_mode)

        except Exception as e:
            error_text = self._safe_str(e)
            if matched_rule_context:
                rule_label = matched_rule_context.get('keyword') or f"规则ID={matched_rule_context.get('id')}"
                card_type = matched_rule_context.get('card_type') or 'unknown'
                error_message = f"规则已命中({rule_label})，但{card_type}发货处理异常: {error_text}"
            else:
                error_message = f"自动发货异常: {error_text}"
            logger.error(error_message)
            return build_result(
                False,
                error=error_message,
                matched_rule=matched_rule_context,
                match_mode_value=match_mode_context
            )



    def _process_delivery_content_with_description(self, delivery_content: str, card_description: str) -> str:
        try:
            if not card_description or not card_description.strip():
                return delivery_content

            processed_description = card_description.replace('{DELIVERY_CONTENT}', delivery_content)

            if '{DELIVERY_CONTENT}' in card_description:
                return processed_description
            else:
                return f"{processed_description}\n\n{delivery_content}"

        except Exception as e:
            logger.error(f"处理备注信息失败: {e}")
            return delivery_content

    def _build_delivery_steps(self, delivery_content: str, card_description: str):
        try:
            raw_content = delivery_content if isinstance(delivery_content, str) else str(delivery_content or '')
            description = (card_description or '').strip()
            steps = []

            if raw_content and not raw_content.startswith("__IMAGE_SEND__"):
                final_text = self._process_delivery_content_with_description(raw_content, description)
                return [{'type': 'text', 'content': final_text}] if final_text else []

            def append_text_step(text: str):
                text = (text or '').strip()
                if text:
                    steps.append({'type': 'text', 'content': text})

            def append_payload_step(payload: str):
                payload = (payload or '').strip()
                if payload:
                    if payload.startswith("__IMAGE_SEND__"):
                        steps.append({'type': 'image', 'content': payload})
                    else:
                        steps.append({'type': 'text', 'content': payload})

            if not description:
                append_payload_step(raw_content)
                return steps

            if '{DELIVERY_CONTENT}' in description:
                placeholder = '{DELIVERY_CONTENT}'
                segments = description.split(placeholder)
                for index, segment in enumerate(segments):
                    append_text_step(segment)
                    if index < len(segments) - 1:
                        append_payload_step(raw_content)
                return steps

            append_text_step(description)
            append_payload_step(raw_content)
            return steps
        except Exception as e:
            logger.error(f"构建发货步骤失败: {e}")
            fallback_content = delivery_content if isinstance(delivery_content, str) else str(delivery_content or '')
            if fallback_content:
                return [{'type': 'image' if fallback_content.startswith("__IMAGE_SEND__") else 'text', 'content': fallback_content}]
            return []

    def _can_batch_text_delivery(self, delivery_steps, card_type: str = None) -> bool:
        normalized_card_type = str(card_type or '').strip().lower()
        if normalized_card_type not in {'text', 'data', 'api'}:
            return False

        steps = delivery_steps or []
        if len(steps) != 1:
            return False

        step = steps[0] or {}
        if step.get('type') != 'text':
            return False

        return bool((step.get('content') or '').strip())

    def _format_delivery_unit_text(self, text: str, unit_index: int, total_units: int) -> str:
        safe_total_units = max(1, int(total_units or 1))
        safe_unit_index = max(1, int(unit_index or 1))
        prefix = f"【{safe_unit_index}/{safe_total_units}】"
        content = (text or '').strip()
        return f"{prefix}{content}" if content else prefix

    def _apply_delivery_unit_numbering(self, delivery_steps, unit_index: int, total_units: int, card_type: str = None):
        if max(1, int(total_units or 1)) <= 1:
            return delivery_steps or []

        normalized_card_type = str(card_type or '').strip().lower()
        if normalized_card_type not in {'text', 'data', 'api'}:
            return delivery_steps or []

        steps = [dict(step or {}) for step in (delivery_steps or [])]
        prefix = f"【{max(1, int(unit_index or 1))}/{max(1, int(total_units or 1))}】"

        for step in steps:
            if step.get('type') == 'text':
                step['content'] = f"{prefix}{(step.get('content') or '').strip()}"
                return steps

        return [{'type': 'text', 'content': prefix}] + steps

    def _build_delivery_send_groups(self, prepared_units, total_units: int,
                                    max_units_per_message: int = DELIVERY_BATCH_MAX_UNITS,
                                    max_chars_per_message: int = DELIVERY_BATCH_MAX_CHARS):
        if max(1, int(total_units or 1)) <= 1:
            return [{
                'mode': 'single',
                'units': [prepared_unit],
                'delivery_steps': prepared_unit.get('delivery_steps') or [],
                'unit_count': 1,
                'char_count': 0,
            } for prepared_unit in sorted(prepared_units or [], key=lambda unit: int(unit.get('unit_index') or 0))]

        groups = []
        current_batch_units = []
        current_batch_chars = 0

        def flush_current_batch():
            nonlocal current_batch_units, current_batch_chars
            if not current_batch_units:
                return

            batched_text = '\n\n'.join(unit['batched_text'] for unit in current_batch_units)
            groups.append({
                'mode': 'batched_text',
                'units': list(current_batch_units),
                'delivery_steps': [{'type': 'text', 'content': batched_text}],
                'unit_count': len(current_batch_units),
                'char_count': len(batched_text),
            })
            current_batch_units = []
            current_batch_chars = 0

        for prepared_unit in sorted(prepared_units or [], key=lambda unit: int(unit.get('unit_index') or 0)):
            delivery_steps = prepared_unit.get('delivery_steps') or []
            rule_meta = prepared_unit.get('rule_meta') or {}
            card_type = prepared_unit.get('card_type') or rule_meta.get('card_type')

            if not self._can_batch_text_delivery(delivery_steps, card_type):
                flush_current_batch()
                numbered_steps = self._apply_delivery_unit_numbering(
                    delivery_steps,
                    prepared_unit.get('unit_index') or 1,
                    total_units,
                    card_type,
                )
                groups.append({
                    'mode': 'single',
                    'units': [prepared_unit],
                    'delivery_steps': numbered_steps,
                    'unit_count': 1,
                    'char_count': 0,
                })
                continue

            numbered_text = self._format_delivery_unit_text(
                delivery_steps[0].get('content') or '',
                prepared_unit.get('unit_index') or 1,
                total_units,
            )

            if len(numbered_text) > max_chars_per_message:
                flush_current_batch()
                logger.warning(
                    f"【{self.account_id}】发货单元 {prepared_unit.get('unit_index')} 文本长度 {len(numbered_text)} 超过批量阈值 {max_chars_per_message}，回退为单条发送"
                )
                groups.append({
                    'mode': 'single',
                    'units': [prepared_unit],
                    'delivery_steps': [{'type': 'text', 'content': numbered_text}],
                    'unit_count': 1,
                    'char_count': len(numbered_text),
                })
                continue

            separator_chars = 2 if current_batch_units else 0
            exceeds_unit_limit = len(current_batch_units) >= max_units_per_message
            exceeds_char_limit = current_batch_units and (
                current_batch_chars + separator_chars + len(numbered_text) > max_chars_per_message
            )

            if exceeds_unit_limit or exceeds_char_limit:
                flush_current_batch()

            prepared_unit_with_text = dict(prepared_unit)
            prepared_unit_with_text['batched_text'] = numbered_text
            current_batch_units.append(prepared_unit_with_text)
            current_batch_chars += (2 if len(current_batch_units) > 1 else 0) + len(numbered_text)

        flush_current_batch()
        return groups

    async def _send_delivery_steps(self, websocket, chat_id: str, user_id: str, delivery_steps, user_url: str = None,
                                   log_prefix: str = "自动发货", card_id: int = None):
        steps = delivery_steps or []
        if not steps:
            raise ValueError("发货步骤为空")

        total_steps = len(steps)
        user_url = user_url or f'https://www.goofish.com/personal?userId={user_id}'

        for index, step in enumerate(steps, start=1):
            step_type = step.get('type')
            step_content = step.get('content') or ''

            if step_type == 'image':
                image_data = step_content.replace("__IMAGE_SEND__", "", 1)
                image_card_id = card_id
                image_url = image_data
                if "|" in image_data:
                    card_id_str, image_url = image_data.split("|", 1)
                    try:
                        image_card_id = int(card_id_str)
                    except ValueError:
                        logger.error(f"无效的卡券ID: {card_id_str}")
                        image_card_id = card_id

                await self.send_image_msg(websocket, chat_id, user_id, image_url, card_id=image_card_id)
                logger.info(
                    f"【{log_prefix}】步骤 {index}/{total_steps} 已向 {user_url} 发送图片: {image_url}"
                )
            else:
                await self.send_msg(websocket, chat_id, user_id, step_content)
                logger.info(
                    f"【{log_prefix}】步骤 {index}/{total_steps} 已向 {user_url} 发送文本内容"
                )

            if total_steps > 1 and index < total_steps:
                await asyncio.sleep(0.3)

    async def _get_api_card_content(self, rule, order_id=None, item_id=None, buyer_id=None, spec_name=None, spec_value=None, retry_count=0):
        max_retries = 4

        if retry_count >= max_retries:
            logger.error(f"API调用失败，已达到最大重试次数({max_retries})")
            return None

        try:
            import aiohttp
            import json

            api_config = rule.get('api_config')
            if not api_config:
                logger.error(f"API配置为空，规则ID: {rule.get('id')}, 卡券名称: {rule.get('card_name')}")
                logger.warning(f"规则详情: {rule}")
                return None

            if isinstance(api_config, str):
                api_config = json.loads(api_config)

            url = api_config.get('url')
            method = api_config.get('method', 'GET').upper()
            timeout = api_config.get('timeout', 10)
            headers = api_config.get('headers', '{}')
            params = api_config.get('params', '{}')

            if isinstance(headers, str):
                headers = json.loads(headers)
            if isinstance(params, str):
                params = json.loads(params)

            if method == 'POST' and params:
                params = await self._replace_api_dynamic_params(params, order_id, item_id, buyer_id, spec_name, spec_value)

            retry_info = f" (重试 {retry_count + 1}/{max_retries})" if retry_count > 0 else ""
            logger.info(f"调用API获取卡券: {method} {url}{retry_info}")
            if method == 'POST' and params:
                logger.warning(f"POST请求参数: {json.dumps(params, ensure_ascii=False)}")

            if not self.session:
                await self.create_session()

            timeout_obj = aiohttp.ClientTimeout(total=timeout)

            if method == 'GET':
                async with self.session.get(url, headers=headers, params=params, timeout=timeout_obj) as response:
                    status_code = response.status
                    response_text = await response.text()
            elif method == 'POST':
                async with self.session.post(url, headers=headers, json=params, timeout=timeout_obj) as response:
                    status_code = response.status
                    response_text = await response.text()
            else:
                logger.error(f"不支持的HTTP方法: {method}")
                return None

            if status_code == 200:
                try:
                    result = json.loads(response_text)
                    if isinstance(result, dict):
                        content = result.get('data') or result.get('content') or result.get('card') or str(result)
                    else:
                        content = str(result)
                except Exception:
                    content = response_text

                logger.info(f"API调用成功，返回内容长度: {len(content)}")
                return content
            else:
                logger.warning(f"API调用失败: {status_code} - {response_text[:200]}...")

                if status_code >= 500 or status_code == 408:
                    if retry_count < max_retries - 1:
                        wait_time = (retry_count + 1) * 2
                        logger.info(f"等待 {wait_time} 秒后重试...")
                        await asyncio.sleep(wait_time)
                        return await self._get_api_card_content(rule, order_id, item_id, buyer_id, spec_name, spec_value, retry_count + 1)

                return None

        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            logger.warning(f"API调用网络异常: {self._safe_str(e)}")

            if retry_count < max_retries - 1:
                wait_time = (retry_count + 1) * 2
                logger.info(f"等待 {wait_time} 秒后重试...")
                await asyncio.sleep(wait_time)
                return await self._get_api_card_content(rule, order_id, item_id, buyer_id, spec_name, spec_value, retry_count + 1)
            else:
                logger.error(f"API调用网络异常，已达到最大重试次数: {self._safe_str(e)}")
                return None

        except Exception as e:
            logger.error(f"API调用异常: {self._safe_str(e)}")
            return None

    async def _get_yifan_api_card_content(self, rule, order_id=None, item_id=None, buyer_id=None, chat_id=None):
        try:
            import hashlib
            import time
            import aiohttp
            import json
            from urllib.parse import urlencode
            canonical_account_id = self._canonical_account_id()

            api_config = rule.get('api_config')
            if not api_config:
                logger.error(f"亦凡API配置为空，规则ID: {rule.get('id')}, 卡券名称: {rule.get('card_name')}")
                return None

            if isinstance(api_config, str):
                api_config = json.loads(api_config)

            user_id = api_config.get('user_id')
            user_key = api_config.get('user_key')
            goods_id = api_config.get('goods_id')
            callback_url = (api_config.get('callback_url') or '').strip() or (YIFAN_API.get('callback_url') or '').strip() or 'http://116.196.116.76/yifan.php'
            require_account = api_config.get('require_account', False)

            if not user_id or not user_key or not goods_id:
                logger.error(f"亦凡API配置不完整，规则ID: {rule.get('id')}")
                return None

            if order_id and not canonical_account_id:
                logger.error(
                    "【default】亦凡订单缺少 canonical account_id，拒绝继续下单"
                )
                return None

            recharge_account = None
            if require_account:
                logger.info(f"亦凡API需要充值账号，开始询问流程")
                recharge_account = await self._ask_for_recharge_account(chat_id, buyer_id, rule, order_id, item_id)
                if recharge_account == "__WAITING_ACCOUNT__":
                    logger.info(f"已设置等待账号输入状态，暂停发货流程")
                    return None
                elif not recharge_account:
                    logger.error(f"获取充账号失败，取消发货")
                    return None
                logger.info(f"获取到充值账号: {recharge_account}")

            timestamp = str(int(time.time()))
            params = {
                'userid': str(user_id),
                'timestamp': timestamp,
                'goodsid': str(goods_id),
                'buynum': '1',
            }

            if callback_url and callback_url.strip():
                params['callbackurl'] = str(callback_url).strip()

            if recharge_account:
                params['attach'] = str(recharge_account).strip()

            sign_params = {k: str(v).strip() for k, v in params.items() if v is not None and str(v).strip() != ''}
            sorted_keys = sorted(sign_params.keys())
            sign_string = '&'.join([f"{key}={sign_params[key]}" for key in sorted_keys])
            sign_string += user_key

            logger.info(f"亦凡API签名字符串: {sign_string}")

            sign = hashlib.md5(sign_string.encode('utf-8')).hexdigest().lower()
            params['sign'] = sign

            logger.info(f"调用亦凡API: 商户ID={user_id}, 商品ID={goods_id}, 充值账号={recharge_account}, 回调URL={callback_url if callback_url else '无'}")

            if not self.session:
                await self.create_session()

            api_url = "http://price.78shuk.top/dockapiv3/order/create"

            timeout_obj = aiohttp.ClientTimeout(total=30)
            async with self.session.post(api_url, data=params, timeout=timeout_obj) as response:
                status_code = response.status
                response_text = await response.text()

                logger.info(f"亦凡API返回状码: {status_code}, 响应: {response_text}")

                if status_code == 200:
                    try:
                        result = json.loads(response_text)
                        if result.get('code') == 1:
                            data = result.get('data', {})
                            order_no = data.get('orderno', '')
                            us_order_no = data.get('usorderno', '')

                            success_msg = f"✅ 自动发货订单已提交成功\n\n"
                            success_msg += f"📋 订单信息：\n"
                            success_msg += f"平台订单号: {order_no}\n"
                            if us_order_no:
                                success_msg += f"商家订单号: {us_order_no}\n"

                            query_url = YIFAN_API.get('query_url', 'http://116.196.116.76/yifan.php')
                            success_msg += f"\n🔍 查询卡密：\n"
                            success_msg += f"{query_url}\n"
                            success_msg += f"(输入订单号查询)\n"

                            success_msg += f"\n⏰ 温馨提示：\n"
                            success_msg += f"订单处理需要一定时间，请耐心等待。\n"
                            success_msg += f"如果1小时后仍未看到卡密信息，\n"
                            success_msg += f"请联系客服处理。"

                            logger.info(f"亦凡API调用成功: order_no={order_no}")

                            if order_id and order_no:
                                try:
                                    from db_manager import db_manager
                                    if not canonical_account_id:
                                        logger.error(
                                            "【default】亦凡订单绑定缺少 canonical account_id，拒绝回填订单归属"
                                        )
                                    else:
                                        db_manager.update_order_yifan_status(
                                            order_id=order_id,
                                            account_id=canonical_account_id,
                                            yifan_orderno=order_no,
                                            delivery_status='processing'
                                        )
                                        if chat_id:
                                            db_manager.update_order_chat_id(order_id, chat_id, account_id=canonical_account_id)
                                        logger.info(f"已记录亦凡订单信息: order_id={order_id}, yifan_orderno={order_no}")
                                except Exception as e:
                                    logger.error(f"记录亦凡订单信息失败: {e}")

                            return success_msg
                        else:
                            error_msg = result.get('msg', '未知错误')
                            logger.error(f"亦凡API调用失败: code={result.get('code')}, msg={error_msg}")

                            if chat_id and buyer_id:
                                from db_manager import db_manager
                                notification_msg = f"❌ 自动发货失败\n错误信息: {error_msg}\n请联系客服处理"
                                await self.send_notification("系统", buyer_id, notification_msg, item_id or "unknown", chat_id)

                            return None
                    except Exception as e:
                        logger.error(f"解析亦凡API返回失败: {self._safe_str(e)}")
                        return None
                else:
                    logger.error(f"亦凡API调用失败: HTTP {status_code} - {response_text[:200]}")
                    return None

        except Exception as e:
            logger.error(f"亦凡API调用异常: {self._safe_str(e)}")
            return None

    async def _call_yifan_api_with_account(self, rule, account, order_id=None, item_id=None, buyer_id=None, chat_id=None):
        try:
            import hashlib
            import time
            import aiohttp
            import json

            api_config = rule.get('api_config')
            if not api_config:
                logger.error(f"亦凡API配置为空")
                return None

            if isinstance(api_config, str):
                api_config = json.loads(api_config)

            user_id = api_config.get('user_id')
            user_key = api_config.get('user_key')
            goods_id = api_config.get('goods_id')
            callback_url = api_config.get('callback_url', '')

            if not user_id or not user_key or not goods_id:
                logger.error(f"亦凡API配置不完整")
                return None

            timestamp = str(int(time.time()))
            params = {
                'userid': str(user_id),
                'timestamp': timestamp,
                'goodsid': str(goods_id),
                'buynum': '1',
                'attach': str(account).strip()
            }

            if callback_url and callback_url.strip():
                params['callbackurl'] = str(callback_url).strip()

            sign_params = {k: str(v).strip() for k, v in params.items() if v is not None and str(v).strip() != ''}
            sorted_keys = sorted(sign_params.keys())
            sign_string = '&'.join([f"{key}={sign_params[key]}" for key in sorted_keys])
            sign_string += user_key

            logger.info(f"亦凡API签名字符串: {sign_string}")

            sign = hashlib.md5(sign_string.encode('utf-8')).hexdigest().lower()
            params['sign'] = sign

            logger.info(f"调用亦凡API: 商户ID={user_id}, 商品ID={goods_id}, 充值账号={account}, 回调URL={callback_url if callback_url else '无'}")

            if not self.session:
                await self.create_session()

            api_url = "http://price.78shuk.top/dockapiv3/order/create"

            timeout_obj = aiohttp.ClientTimeout(total=30)
            async with self.session.post(api_url, data=params, timeout=timeout_obj) as response:
                status_code = response.status
                response_text = await response.text()

                logger.info(f"亦凡API返回状码: {status_code}, 响应: {response_text}")

                if status_code == 200:
                    try:
                        result = json.loads(response_text)
                        if result.get('code') == 1:
                            data = result.get('data', {})
                            order_no = data.get('orderno', '')
                            us_order_no = data.get('usorderno', '')

                            success_msg = f"✅ 下单成功\n"
                            success_msg += f"订单号: {order_no}\n"
                            if us_order_no:
                                success_msg += f"用户订单号: {us_order_no}\n"
                            success_msg += f"充值账号: {account}\n"
                            success_msg += f"返回信息: {result.get('msg', '提交成功')}\n"
                            success_msg += f"有任何问题，请及时联系客服处理。"

                            logger.info(f"亦凡API调用成功: {success_msg}")
                            return success_msg
                        else:
                            error_msg = result.get('msg', '未知错误')
                            logger.error(f"亦凡API调用失败: code={result.get('code')}, msg={error_msg}")

                            if chat_id and buyer_id:
                                from db_manager import db_manager
                                notification_msg = f"❌ 自动发货失败\n错误信息: {error_msg}\n请联系客服处理"
                                await self.send_notification("系统", buyer_id, notification_msg, item_id or "unknown", chat_id)

                            return None
                    except Exception as e:
                        logger.error(f"解析亦凡API返回失败: {self._safe_str(e)}")
                        return None
                else:
                    logger.error(f"亦凡API调用失败: HTTP {status_code} - {response_text[:200]}")
                    return None

        except Exception as e:
            logger.error(f"亦凡API调用异常: {self._safe_str(e)}")
            return None

    async def _ask_for_recharge_account(self, chat_id, buyer_id, rule, order_id=None, item_id=None):
        try:
            async with self.yifan_account_lock:
                self.yifan_account_waiting[chat_id] = {
                    'buyer_id': buyer_id,
                    'rule': rule,
                    'order_id': order_id,
                    'item_id': item_id,
                    'state': 'waiting_account',
                    'account': None,
                    'create_time': time.time(),
                    'retry_count': 0
                }

            ask_message = "请单独发送您的充值账号，不要有任何其他的文字。如果因为您输错的原因导致错误下单，概不退款。"
            await self.send_msg(self.ws, chat_id, buyer_id, ask_message)
            logger.info(f"已发送充值账号询问消息，等待用户回复")

            return "__WAITING_ACCOUNT__"

        except Exception as e:
            logger.error(f"询问充值账号异常: {self._safe_str(e)}")
            return None

    async def _replace_api_dynamic_params(self, params, order_id=None, item_id=None, buyer_id=None, spec_name=None, spec_value=None):
        try:
            if not params or not isinstance(params, dict):
                return params

            canonical_account_id = self._canonical_account_id()

            order_info = None
            item_info = None

            needs_account_scoped_lookup = bool(order_id or item_id)
            if needs_account_scoped_lookup and not canonical_account_id:
                logger.warning(
                    "API动态参数替换缺少 canonical account_id，跳过订单/商品读取"
                )
            else:
                from db_manager import db_manager

                if order_id:
                    try:
                        order_info = db_manager.get_order_by_id(order_id, account_id=canonical_account_id)
                        if order_info:
                            logger.warning(f"从数据库获取到订单信息: {order_id}")
                        else:
                            logger.warning(f"无法从数据库获取订单信息: {order_id}")
                    except Exception as e:
                        logger.warning(f"获取订单信息失败: {self._safe_str(e)}")

                if item_id:
                    try:
                        item_info = db_manager.get_item_info(canonical_account_id, item_id)
                        if item_info:
                            logger.warning(f"从数据库获取到商品信息: {item_id}")
                        else:
                            logger.warning(f"无法从数据库获取商品信息: {item_id}")
                    except Exception as e:
                        logger.warning(f"获取商品信息失败: {self._safe_str(e)}")

            param_mapping = {
                'account_id': canonical_account_id or '',
                'order_id': order_id or '',
                'item_id': item_id or '',
                'buyer_id': buyer_id or '',
                'spec_name': spec_name or '',
                'spec_value': spec_value or '',
                'timestamp': str(int(time.time())),
            }

            if order_info:
                param_mapping.update({
                    'order_amount': str(order_info.get('amount', '')),
                    'order_quantity': str(order_info.get('quantity', '')),
                })

            if item_info:
                item_detail = item_info.get('item_detail', '')
                if item_detail:
                    try:
                        import json
                        detail_data = json.loads(item_detail)
                        if isinstance(detail_data, dict) and 'detail' in detail_data:
                            item_detail = detail_data['detail']
                    except (json.JSONDecodeError, TypeError):
                        pass

                param_mapping.update({
                    'item_detail': item_detail,
                })

            replaced_params = self._recursive_replace_params(params, param_mapping)

            replaced_keys = []
            for key, value in replaced_params.items():
                if isinstance(value, str) and '{' in str(params.get(key, '')):
                    replaced_keys.append(key)

            if replaced_keys:
                logger.info(f"API动态参数替换完成，替换的参数: {replaced_keys}")
                logger.warning(f"参数映射: {param_mapping}")

            return replaced_params

        except Exception as e:
            logger.error(f"替换API动态参数失败: {self._safe_str(e)}")
            return params

    def _recursive_replace_params(self, obj, param_mapping):
        if isinstance(obj, dict):
            result = {}
            for key, value in obj.items():
                result[key] = self._recursive_replace_params(value, param_mapping)
            return result
        elif isinstance(obj, list):
            return [self._recursive_replace_params(item, param_mapping) for item in obj]
        elif isinstance(obj, str):
            result = obj
            for param_key, param_value in param_mapping.items():
                placeholder = f"{{{param_key}}}"
                if placeholder in result:
                    result = result.replace(placeholder, str(param_value))
            return result
        else:
            return obj

    async def token_refresh_loop(self):
        try:
            while True:
                try:
                    if not self._is_current_account_enabled():
                        logger.info(f"【{self.account_id}】账号已禁用，停止Token刷新循环")
                        break

                    current_time = time.time()
                    if current_time - self.last_session_keepalive_time >= self.session_keepalive_interval:
                        logger.info(f"【{self.account_id}】开始执行轻量会话保活...")
                        keepalive_ok = await self.keep_session_alive()
                        if keepalive_ok:
                            await self._interruptible_sleep(60)
                            continue

                        keepalive_status = getattr(self, 'last_session_keepalive_status', None)
                        if keepalive_status == "auth_failed":
                            logger.warning(f"【{self.account_id}】轻量保活鉴权失败，尝试执行重型Token恢复流程")
                            new_token = await self.refresh_token()
                            if new_token:
                                self.last_session_keepalive_time = time.time()
                                logger.info(f"【{self.account_id}】重型Token恢复成功，主动关闭旧WebSocket以使用新Token重连")
                                await self._force_websocket_reconnect("重型Token恢复成功，准备使用新Token重连")
                                break

                            last_refresh_status = getattr(self, 'last_token_refresh_status', None)
                            benign_refresh_statuses = ("skipped_cooldown", "restarted_after_cookie_refresh")
                            if last_refresh_status not in benign_refresh_statuses:
                                scheduled_error_message = self._build_scheduled_token_refresh_error_message(last_refresh_status)
                                await self.send_token_refresh_notification(
                                    scheduled_error_message,
                                    "token_scheduled_refresh_failed"
                                )
                            logger.warning(
                                f"【{self.account_id}】重型Token恢复失败(status={last_refresh_status})，"
                                f"{self.token_retry_interval} 秒后重试"
                            )
                            await self._interruptible_sleep(self.token_retry_interval)
                        else:
                            logger.warning(
                                f"【{self.account_id}】轻量保活失败(status={keepalive_status})，"
                                f"{self.session_keepalive_retry_interval} 秒后重试"
                            )
                            await self._interruptible_sleep(self.session_keepalive_retry_interval)
                        continue
                    await self._interruptible_sleep(60)
                except asyncio.CancelledError:
                    logger.info(f"【{self.account_id}】Token刷新循环收到取消信号，准备退出")
                    raise
                except Exception as e:
                    logger.error(f"Token刷新循环出错: {self._safe_str(e)}")
                    try:
                        await self._interruptible_sleep(60)
                    except asyncio.CancelledError:
                        logger.info(f"【{self.account_id}】Token刷新循环在重试等待时收到取消信号，准备退出")
                        raise
        except asyncio.CancelledError:
            logger.info(f"【{self.account_id}】Token刷新循环已取消，正在退出...")
            raise
        finally:
            logger.info(f"【{self.account_id}】Token刷新循环已退出")

    async def create_chat(self, ws, toid, item_id='891198795482'):
        msg = {
            "lwp": "/r/SingleChatConversation/create",
            "headers": {
                "mid": generate_mid()
            },
            "body": [
                {
                    "pairFirst": f"{toid}@goofish",
                    "pairSecond": f"{self.myid}@goofish",
                    "bizType": "1",
                    "extension": {
                        "itemId": item_id
                    },
                    "ctx": {
                        "appVersion": "1.0",
                        "platform": "web"
                    }
                }
            ]
        }
        await ws.send(json.dumps(msg))

    async def send_msg(self, ws, cid, toid, text):
        text = {
            "contentType": 1,
            "text": {
                "text": text
            }
        }
        text_base64 = str(base64.b64encode(json.dumps(text).encode('utf-8')), 'utf-8')
        msg = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {
                "mid": generate_mid()
            },
            "body": [
                {
                    "uuid": generate_uuid(),
                    "cid": f"{cid}@goofish",
                    "conversationType": 1,
                    "content": {
                        "contentType": 101,
                        "custom": {
                            "type": 1,
                            "data": text_base64
                        }
                    },
                    "redPointPolicy": 0,
                    "extension": {
                        "extJson": "{}"
                    },
                    "ctx": {
                        "appVersion": "1.0",
                        "platform": "web"
                    },
                    "mtags": {},
                    "msgReadStatusSetting": 1
                },
                {
                    "actualReceivers": [
                        f"{toid}@goofish",
                        f"{self.myid}@goofish"
                    ]
                }
            ]
        }
        await ws.send(json.dumps(msg))

    async def init(self, ws):
        token_refresh_attempted = False
        current_account_id = self._canonical_account_id()
        if not self.current_token or (time.time() - self.last_token_refresh_time) >= self.token_refresh_interval:
            logger.info(f"【{self.account_id}】获取初始token...")
            token_refresh_attempted = True

            allow_password_login_recovery = True
            manual_refresh_state = self.get_manual_refresh_state(current_account_id)
            qr_login_grace = self.get_qr_login_grace(current_account_id)
            if (
                (manual_refresh_state and manual_refresh_state.get('phase') == 'handoff_recovery')
                or (qr_login_grace and qr_login_grace.get('stage') == 'real_cookie_ready')
            ):
                allow_password_login_recovery = False
                logger.info(
                    f"[{self.account_id}] handoff recovery init token refresh skips password-login recovery"
                )

            await self.refresh_token(
                allow_password_login_recovery=allow_password_login_recovery
            )

        if not self.current_token:
            self.last_init_failure_type = 'init_auth_failed'
            self.last_init_failure_reason = self.last_token_refresh_status or 'token_missing_after_refresh'
            logger.error(f"【{self.account_id}】无法获取有效token，初始化鉴权失败")
            if not token_refresh_attempted:
                await self.send_token_refresh_notification("初始化时无法获取有效Token", "token_init_failed")
            else:
                logger.info(f"【{self.account_id}】由于刚刚尝试过token刷新，跳过重复的初始化失败知")
            raise InitAuthError(f"Token获取失败(status={self.last_init_failure_reason})")

        self.last_init_failure_type = None
        self.last_init_failure_reason = None
        if current_account_id:
            self.clear_init_auth_failure_state(current_account_id)
        self.init_auth_failures = 0

        msg = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": APP_CONFIG.get('app_key'),
                "token": self.current_token,
                "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5",
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": self.device_id,
                "mid": generate_mid()
            }
        }
        await ws.send(json.dumps(msg))
        await asyncio.sleep(1)
        current_time = int(time.time() * 1000)
        msg = {
            "lwp": "/r/SyncStatus/ackDiff",
            "headers": {"mid": generate_mid()},
            "body": [
                {
                    "pipeline": "sync",
                    "tooLong2Tag": "PNM,1",
                    "channel": "sync",
                    "topic": "sync",
                    "highPts": 0,
                    "pts": current_time * 1000,
                    "seq": 0,
                    "timestamp": current_time
                }
            ]
        }
        await ws.send(json.dumps(msg))
        logger.info(f'【{self.account_id}】连接注册完成')

    async def list_all_conversations(self, cid: str, page_size: int = 20):
        headers = self._build_websocket_headers()
        async with await self._create_websocket_connection(headers) as websocket:
            await self.init(websocket)
            send_mid = generate_mid()
            request_msg = {
                "lwp": "/r/MessageManager/listUserMessages",
                "headers": {
                    "mid": send_mid
                },
                "body": [
                    f"{cid}@goofish",
                    False,
                    9007199254740991,
                    page_size,
                    False
                ]
            }
            history_messages = []

            async for raw_message in websocket:
                try:
                    message = json.loads(raw_message)
                except Exception:
                    continue

                try:
                    ack = {
                        "code": 200,
                        "headers": {
                            "mid": message.get("headers", {}).get("mid", generate_mid()),
                            "sid": message.get("headers", {}).get("sid", ""),
                        }
                    }
                    if 'app-key' in message.get("headers", {}):
                        ack["headers"]["app-key"] = message["headers"]["app-key"]
                    if 'ua' in message.get("headers", {}):
                        ack["headers"]["ua"] = message["headers"]["ua"]
                    if 'dt' in message.get("headers", {}):
                        ack["headers"]["dt"] = message["headers"]["dt"]
                    await websocket.send(json.dumps(ack))
                except Exception:
                    pass

                try:
                    if message.get('lwp') == "/s/vulcan":
                        await websocket.send(json.dumps(request_msg))
                        continue

                    recv_mid = message.get("headers", {}).get("mid", "")
                    if recv_mid != send_mid:
                        continue

                    body = message.get("body", {})
                    has_more = body.get("hasMore") == 1
                    next_cursor = body.get("nextCursor")
                    for user_message in body.get("userMessageModels", []):
                        extension = user_message.get("message", {}).get("extension", {})
                        custom_content = user_message.get("message", {}).get("content", {}).get("custom", {})
                        send_message_base64 = custom_content.get("data", "")
                        parsed_message = None
                        if send_message_base64:
                            try:
                                parsed_message = json.loads(base64.b64decode(send_message_base64).decode('utf-8'))
                            except Exception:
                                parsed_message = {"raw": send_message_base64}

                        history_messages.insert(0, {
                            "send_user_id": extension.get("senderUserId", ""),
                            "send_user_name": extension.get("reminderTitle", ""),
                            "message": parsed_message,
                        })

                    if has_more:
                        send_mid = generate_mid()
                        request_msg["headers"]["mid"] = send_mid
                        request_msg["body"][2] = next_cursor
                        await websocket.send(json.dumps(request_msg))
                    else:
                        return history_messages
                except Exception as e:
                    logger.warning(f"【{self.account_id}】拉取历史消息时发生异常: {self._safe_str(e)}")
                    return history_messages

        return []

    async def send_heartbeat(self, ws):
        if ws.closed:
            raise ConnectionError("WebSocket连接已关闭，无法发送心跳")

        heartbeat_mid = generate_mid()
        msg = {
            "lwp": "/!",
            "headers": {
                "mid": heartbeat_mid
            }
        }
        try:
            self.last_sent_heartbeat_mid = heartbeat_mid
            self.pending_heartbeat_mids.append(heartbeat_mid)
            await asyncio.wait_for(ws.send(json.dumps(msg)), timeout=2.0)
            self.last_heartbeat_time = time.time()
            logger.warning(f"【{self.account_id}】心跳包已发送 [ID:{heartbeat_mid}]")
        except asyncio.TimeoutError:
            raise ConnectionError("心跳发送超时，WebSocket可能已断开")
        except asyncio.CancelledError:
            raise

    async def heartbeat_loop(self, ws):
        consecutive_failures = 0
        max_failures = 3

        try:
            while True:
                try:
                    if not self._is_current_account_enabled():
                        logger.info(f"【{self.account_id}】账号已禁用，停止心跳循环")
                        break

                    if ws.closed:
                        logger.warning(f"【{self.account_id}】WebSocket连接已关闭，停止心跳循环")
                        break

                    await self.send_heartbeat(ws)
                    consecutive_failures = 0

                    await self._interruptible_sleep(self.heartbeat_interval)

                except asyncio.CancelledError:
                    logger.info(f"【{self.account_id}】心跳循环收到取消信号，准备退出")
                    raise
                except Exception as e:
                    consecutive_failures += 1
                    logger.error(f"心跳发送失败 ({consecutive_failures}/{max_failures}): {self._safe_str(e)}")

                    if consecutive_failures >= max_failures:
                        logger.error(f"【{self.account_id}】心跳连续失败{max_failures}次，停止心跳循环")
                        break

                    try:
                        await self._interruptible_sleep(5)
                    except asyncio.CancelledError:
                        logger.info(f"【{self.account_id}】心跳循环在重试等待时收到取消信号，准备退出")
                        raise
        except asyncio.CancelledError:
            logger.info(f"【{self.account_id}】心跳循环已取消，正在退出...")
            raise
        finally:
            logger.info(f"【{self.account_id}】心跳循环已退出")

    async def handle_heartbeat_response(self, message_data):
        try:
            if not isinstance(message_data, dict):
                return False

            if message_data.get("code") != 200:
                return False

            if self.is_sync_package(message_data):
                return False

            headers = message_data.get("headers")
            if not isinstance(headers, dict):
                return False

            response_mid = str(headers.get("mid") or "")
            if not response_mid or response_mid not in self.pending_heartbeat_mids:
                return False

            self.last_heartbeat_response = time.time()
            try:
                self.pending_heartbeat_mids.remove(response_mid)
            except ValueError:
                pass
            logger.warning(f"【{self.account_id}】心跳响应正常 [ID:{response_mid}]")
            return True
        except Exception as e:
            logger.error(f"处理心跳响应出错: {self._safe_str(e)}")
        return False

    async def pause_cleanup_loop(self):
        try:
            while True:
                try:
                    if not self._is_current_account_enabled():
                        logger.info(f"【{self.account_id}】账号已禁用，停止清理循环")
                        break

                    pause_manager.cleanup_expired_pauses()
                    await asyncio.sleep(0)
                    self.cleanup_expired_locks(max_age_hours=24)
                    await asyncio.sleep(0)
                    try:
                        cleaned_count = await self._cleanup_item_cache()
                        if cleaned_count > 0:
                            logger.info(f"【{self.account_id}】清理了 {cleaned_count} 个过期的商品详情缓存")
                    except asyncio.CancelledError:
                        raise
                    except Exception as cache_clean_e:
                        logger.warning(f"【{self.account_id}】清理商品详情缓存时出错: {cache_clean_e}")

                    self._cleanup_instance_caches()
                    await asyncio.sleep(0)
                    try:
                        from utils.qr_login import qr_login_manager
                        qr_login_manager.cleanup_expired_sessions()
                        await asyncio.sleep(0)
                    except asyncio.CancelledError:
                        raise
                    except Exception as qr_clean_e:
                        logger.warning(f"【{self.account_id}】清理QR登录会话时出错: {qr_clean_e}")

                    try:
                        await self._cleanup_playwright_cache()
                    except asyncio.CancelledError:
                        raise
                    except Exception as pw_clean_e:
                        logger.warning(f"【{self.account_id}】清理Playwright缓存时出错: {pw_clean_e}")

                    try:
                        cleaned_logs = await self._cleanup_old_logs(retention_days=7)
                        await asyncio.sleep(0)
                    except asyncio.CancelledError:
                        raise
                    except Exception as log_clean_e:
                        logger.warning(f"【{self.account_id}】清理日志文件时出错: {log_clean_e}")

                    try:
                        canonical_account_id = self._canonical_account_id()
                        if canonical_account_id:
                            last_cleanup_times = getattr(self.__class__, '_last_risk_log_cleanup_times', None)
                            if not isinstance(last_cleanup_times, dict):
                                last_cleanup_times = {}
                                self.__class__._last_risk_log_cleanup_times = last_cleanup_times
                            cleanup_locks = getattr(self.__class__, '_risk_log_cleanup_locks', None)
                            if not isinstance(cleanup_locks, dict):
                                cleanup_locks = {}
                                self.__class__._risk_log_cleanup_locks = cleanup_locks

                            cleanup_lock = cleanup_locks.get(canonical_account_id)
                            if cleanup_lock is None:
                                cleanup_lock = asyncio.Lock()
                                cleanup_locks[canonical_account_id] = cleanup_lock

                            async with cleanup_lock:
                                last_risk_cleanup = float(last_cleanup_times.get(canonical_account_id, 0) or 0)
                                current_time = time.time()
                                if current_time - last_risk_cleanup > 600:
                                    try:
                                        cleaned_count = await asyncio.to_thread(
                                            db_manager.mark_stale_risk_control_logs_failed,
                                            timeout_minutes=15,
                                            account_id=canonical_account_id,
                                        )
                                        if cleaned_count > 0:
                                            logger.warning(
                                                f"【{canonical_account_id}】风控日志超时兜底清理完成，自动关闭 {cleaned_count} 条processing记录"
                                            )
                                        last_cleanup_times[canonical_account_id] = current_time
                                    except asyncio.CancelledError:
                                        logger.warning(f"【{canonical_account_id}】风控日志超时兜底清理被取消")
                                        raise
                    except asyncio.CancelledError:
                        raise
                    except Exception as risk_clean_e:
                        logger.error(f"【{self.account_id}】清理超时风控日志时出错: {risk_clean_e}")

                    try:
                        if hasattr(self.__class__, '_last_db_cleanup_time'):
                            last_cleanup = self.__class__._last_db_cleanup_time
                        else:
                            self.__class__._last_db_cleanup_time = 0
                            last_cleanup = 0

                        current_time = time.time()
                        if current_time - last_cleanup > 86400:
                            logger.info(f"【{self.account_id}】开始执行数据库历史数据清理...")
                            try:
                                stats = await asyncio.to_thread(db_manager.cleanup_old_data, days=90)
                                if 'error' not in stats:
                                    logger.info(f"【{self.account_id}】数据库清理完成: {stats}")
                                    self.__class__._last_db_cleanup_time = current_time
                                else:
                                    logger.error(f"【{self.account_id}】数据库清理失败: {stats['error']}")
                            except asyncio.CancelledError:
                                logger.warning(f"【{self.account_id}】数据库清理被取消")
                                raise
                    except asyncio.CancelledError:
                        raise
                    except Exception as db_clean_e:
                        logger.error(f"【{self.account_id}】清理数据库历史数据时出错: {db_clean_e}")

                    await self._interruptible_sleep(300)
                except asyncio.CancelledError:
                    logger.info(f"【{self.account_id}】清理循环收到取消信号，准备退出")
                    raise
                except Exception as e:
                    logger.error(f"【{self.account_id}】清理任务失败: {self._safe_str(e)}")
                    try:
                        await self._interruptible_sleep(300)
                    except asyncio.CancelledError:
                        logger.info(f"【{self.account_id}】清理循环在重试等待时收到取消信号，准备退出")
                        raise
        except asyncio.CancelledError:
            logger.info(f"【{self.account_id}】清理循环已取消，正在退出...")
            raise
        finally:
            logger.info(f"【{self.account_id}】清理循环已退出")


    async def cookie_refresh_loop(self):
        try:
            canonical_account_id = self._canonical_account_id()
            if not canonical_account_id:
                logger.error("【default】Cookie刷新循环缺少 canonical account_id，拒绝继续运行")
                return
            current_account_id = canonical_account_id
            while True:
                try:
                    if not self._is_current_account_enabled():
                        logger.info(f"【{self.account_id}】账号已禁用，停止Cookie刷新循环")
                        break

                    if not self.cookie_refresh_enabled:
                        logger.warning(f"【{self.account_id}】Cookie刷新功能已禁用，跳过执行")
                        await self._interruptible_sleep(300)
                        continue

                    if self.is_manual_refresh_active(current_account_id):
                        logger.warning(f"【{self.account_id}】手动刷新进行中，跳过自动Cookie刷新")
                        await self._interruptible_sleep(60)
                        continue

                    current_time = time.time()
                    if current_time - self.last_cookie_refresh_time >= self.cookie_refresh_interval:
                        time_since_last_message = current_time - self.last_message_received_time
                        if time_since_last_message < self.message_cookie_refresh_cooldown:
                            remaining_time = self.message_cookie_refresh_cooldown - time_since_last_message
                            remaining_minutes = int(remaining_time // 60)
                            remaining_seconds = int(remaining_time % 60)
                            logger.warning(f"【{self.account_id}】收到消息后冷却中，还需等待 {remaining_minutes}分{remaining_seconds}秒 才能执行Cookie刷新")
                        elif self.cookie_refresh_lock.locked():
                            logger.warning(f"【{self.account_id}】Cookie刷新任务已在执行中，跳过本次触发")
                        else:
                            logger.info(f"【{self.account_id}】开始执行Cookie刷新任务...")
                            asyncio.create_task(self._execute_cookie_refresh(current_time))

                    await self._interruptible_sleep(60)
                except asyncio.CancelledError:
                    logger.info(f"【{self.account_id}】Cookie刷新循环收到取消信号，准备退出")
                    raise
                except Exception as e:
                    logger.error(f"【{self.account_id}】Cookie刷新循环失败: {self._safe_str(e)}")
                    try:
                        await self._interruptible_sleep(60)
                    except asyncio.CancelledError:
                        logger.info(f"【{self.account_id}】Cookie刷新循环在重试等待时收到取消信号，准备退出")
                        raise
        except asyncio.CancelledError:
            logger.info(f"【{self.account_id}】Cookie刷新循环已取消，正在退出...")
            raise
        finally:
            logger.info(f"【{self.account_id}】Cookie刷新循环已退出")

    def _prime_cookie_refresh_schedule_on_startup(self) -> bool:
        if self.last_cookie_refresh_time > 0:
            return False

        self.last_cookie_refresh_time = time.time()
        logger.info(
            f"【{self.account_id}】新实例启动时初始化 Cookie 刷新基线，避免接管后立刻又触发一次浏览器刷新"
        )
        return True

    async def _execute_cookie_refresh(self, current_time):

        async with self.cookie_refresh_lock:
            clear_message_received_flag = False
            refresh_flow_entered = False
            try:
                canonical_account_id = self._canonical_account_id()
                if not canonical_account_id:
                    logger.error("【default】执行Cookie刷新任务缺少 canonical account_id，拒绝继续运行")
                    return
                current_account_id = canonical_account_id
                if self.is_manual_refresh_active(current_account_id):
                    logger.warning(f"【{self.account_id}】手动刷新进行中，取消当前自动Cookie刷新任务")
                    return

                refresh_flow_entered = True
                logger.info(f"【{self.account_id}】开始Cookie刷新任务，暂时暂停心跳以避免连接冲突...")

                heartbeat_was_running = False
                if self.heartbeat_task and not self.heartbeat_task.done():
                    heartbeat_was_running = True
                    self.heartbeat_task.cancel()
                    logger.warning(f"【{self.account_id}】已暂停心跳任务")

                success = await asyncio.wait_for(
                    self._refresh_cookies_via_browser(),
                    timeout=180.0
                    )

                if heartbeat_was_running and self.ws and not self.ws.closed:
                    logger.warning(f"【{self.account_id}】重新启动心跳任务")
                    self.heartbeat_task = asyncio.create_task(self.heartbeat_loop(self.ws))

                if success:
                    self.last_cookie_refresh_time = current_time
                    logger.info(f"【{self.account_id}】Cookie刷新任务完成，心跳已恢复")

                    logger.info(f"【{self.account_id}】开始验证刷新后的Cookie有效性...")
                    try:
                        validation_result = await self._verify_cookie_validity()

                        if not validation_result['valid']:
                            logger.warning(f"【{self.account_id}】❌ Cookie验证失败: {validation_result['details']}")
                            if validation_result.get('relogin_recommended', True):
                                logger.warning(f"【{self.account_id}】检测到Cookie可能无法用于关键API，尝试过密码登录重新获取...")

                                password_refresh_success = await self._try_password_login_refresh("Cookie验证失败(关键API不可用)")

                                if password_refresh_success:
                                    logger.info(f"【{self.account_id}】✅ 密码登录刷新成功，Cookie已更新")
                                    clear_message_received_flag = True
                                else:
                                    logger.warning(f"【{self.account_id}】⚠️ 密码登录刷新失败，Cookie可能仍然无效")
                                    await self.send_token_refresh_notification(
                                        f"Cookie验证失败且密码登录刷新也失败\n验证详情: {validation_result['details']}",
                                        "cookie_validation_failed"
                                    )
                            else:
                                logger.warning(f"【{self.account_id}】Cookie验证失败，但当前错误更像网络/环境问题，跳过密码登录刷新")
                        else:
                            if validation_result.get('inconclusive'):
                                logger.warning(f"【{self.account_id}】⚠️ Cookie验证结果不确定，保留当前消息冷却标志，等待后续保活再次确认: {validation_result['details']}")
                            else:
                                logger.info(f"【{self.account_id}】✅ Cookie验证通过: {validation_result['details']}")
                                clear_message_received_flag = True

                    except Exception as verify_e:
                        logger.error(f"【{self.account_id}】Cookie验证过程异常: {self._safe_str(verify_e)}")
                        import traceback
                        logger.error(f"【{self.account_id}】详细堆栈:\n{traceback.format_exc()}")
                else:
                    logger.warning(f"【{self.account_id}】Cookie刷新任务失败")
                    self.last_cookie_refresh_time = current_time

            except asyncio.TimeoutError:
                self.last_cookie_refresh_time = current_time
            except Exception as e:
                logger.error(f"【{self.account_id}】执行Cookie刷新任务异常: {self._safe_str(e)}")
                self.last_cookie_refresh_time = current_time
            finally:
                if (refresh_flow_entered and self.ws and not self.ws.closed and
                    (not self.heartbeat_task or self.heartbeat_task.done())):
                    logger.info(f"【{self.account_id}】Cookie刷新完成，心跳任务正常运行")
                    self.heartbeat_task = asyncio.create_task(self.heartbeat_loop(self.ws))

                if clear_message_received_flag:
                    self.last_message_received_time = 0
                    logger.warning(f"【{self.account_id}】Cookie刷新完成，已清空消息接收标志")
                else:
                    logger.warning(f"【{self.account_id}】Cookie刷新未确认恢复可用，保留消息接收标志")



    def enable_cookie_refresh(self, enabled: bool = True):
        self.cookie_refresh_enabled = enabled
        status = "启用" if enabled else "禁用"
        logger.info(f"【{self.account_id}】Cookie刷新功能已{status}")


    async def refresh_cookies_from_qr_login(
        self,
        qr_cookies_str: str,
        user_id: int = None,
        managed_runtime_lease=None,
        managed_runtime=None,
        managed_context=None,
        managed_page=None,
        account_id: str = None,
    ):
        browser = managed_runtime
        context = managed_context
        page = managed_page
        runtime_lease = managed_runtime_lease
        owns_runtime_lease = False
        close_browser = False
        close_context = False
        close_page = False
        explicit_account_id = self._normalize_account_scope(account_id)
        canonical_account_id = self._canonical_account_id()
        target_account_id = explicit_account_id or canonical_account_id
        if not target_account_id:
            logger.error("【default】扫码登录Cookie刷新缺少 account_id，无法继续申请账号级浏览器 runtime")
            return False
        if explicit_account_id and canonical_account_id and explicit_account_id != canonical_account_id:
            logger.error(
                f"【{canonical_account_id}】扫码登录Cookie刷新拒绝跨账号请求: "
                f"account_id={explicit_account_id}"
                )
            return False
        if runtime_lease is not None:
            lease_account_id = self._normalize_account_scope(getattr(runtime_lease, "account_id", None))
            if not lease_account_id:
                logger.warning(
                    f"【{target_account_id}】扫码登录Cookie刷新收到缺少 account_id 的 managed_runtime_lease，"
                    "放弃上游 handoff，改走账号持久化画像"
                )
                browser = None
                context = None
                page = None
                runtime_lease = None
            elif lease_account_id != target_account_id:
                logger.error(
                    f"【{target_account_id}】扫码登录Cookie刷新拒绝跨账号 managed runtime handoff: "
                    f"lease_account_id={lease_account_id}"
                )
                browser = None
                context = None
                page = None
                runtime_lease = None
            elif getattr(runtime_lease, "released", False):
                logger.warning(
                    f"【{target_account_id}】扫码登录Cookie刷新收到已释放的 managed_runtime_lease，"
                    "放弃上游 handoff，重新申请同账号 runtime"
                )
                browser = None
                context = None
                page = None
                runtime_lease = None
            else:
                lease_runtime = getattr(runtime_lease, "runtime", None)
                lease_context = getattr(lease_runtime, "context", None)
                lease_browser = getattr(lease_context, "browser", None) or getattr(lease_runtime, "browser", None)
                if lease_context is None:
                    reason_suffix = (
                        "，且传入的 managed_context 无法证明归属于该 lease"
                        if context is not None else ""
                    )
                    logger.warning(
                        f"【{target_account_id}】扫码登录Cookie刷新收到缺少 context 的 managed_runtime_lease"
                        f"{reason_suffix}。"
                        "先释放失效 handoff lease，再重新申请同账号 runtime"
                    )
                    try:
                        await account_browser_runtime_manager.release_runtime(
                            runtime_lease,
                            reason="qr_cookie_refresh_invalid_handoff_lease",
                        )
                    except Exception as release_error:
                        logger.warning(
                            f"【{target_account_id}】释放失效 handoff lease 失败，继续按账号持久化画像重试: "
                            f"{self._safe_str(release_error)}"
                        )
                    browser = None
                    context = None
                    page = None
                    runtime_lease = None
                elif context is not None and context is not lease_context:
                    logger.warning(
                        f"【{target_account_id}】扫码登录Cookie刷新收到不属于当前 runtime lease 的 managed_context，"
                        "忽略上游页面句柄并回退到 lease 自身上下文"
                    )
                    browser = lease_browser
                    context = lease_context
                    page = None
                if runtime_lease is not None:
                    lease_pages = getattr(runtime_lease, "pages", None)
                    if page is not None:
                        if not isinstance(lease_pages, list):
                            logger.warning(
                                f"【{target_account_id}】扫码登录Cookie刷新收到缺少 pages 跟踪信息的 managed_runtime_lease，"
                                "不复用上游 managed_page，改为向同账号 lease 申请 fresh page"
                            )
                            page = None
                        elif page not in lease_pages:
                            logger.warning(
                                f"【{target_account_id}】扫码登录Cookie刷新收到未被 runtime lease 跟踪的 managed_page，"
                                "改为向同账号 lease 申请 fresh page"
                            )
                            page = None
        elif context is not None:
            logger.warning(
                f"【{target_account_id}】扫码登录Cookie刷新传入 managed_context 但缺少 managed_runtime_lease，"
                "无法校验账号归属，改走账号持久化画像"
            )
            browser = None
            context = None
            page = None
        target_user_id = user_id or self.user_id

        try:
            if explicit_account_id and not canonical_account_id:
                logger.error(
                    f"【{target_account_id}】扫码登录Cookie刷新拒绝缺少 canonical account_id 的实例接管显式账号请求"
                )
                return False
            from utils.xianyu_utils import trans_cookies

            logger.info(f"【{target_account_id}】开始使用扫码登录cookie获取真实cookie...")
            logger.info(f"【{target_account_id}】扫码cookie长度: {len(qr_cookies_str)}")

            qr_cookies_dict = trans_cookies(qr_cookies_str)
            logger.info(f"【{target_account_id}】扫码 Cookie 字段数: {len(qr_cookies_dict)}")

            if runtime_lease is not None and context is None:
                runtime = getattr(runtime_lease, "runtime", None)
                context = getattr(runtime, "context", None)
                browser = browser or getattr(context, "browser", None) or getattr(runtime, "browser", None)

            if page is not None and context is None:
                logger.warning(f"【{target_account_id}】传入 managed_page 但缺少 managed_context，忽略该页面复用")
                page = None

            if context is None:
                if browser is not None:
                    logger.warning(
                        f"【{target_account_id}】传入 managed_runtime 但缺少 managed_context，"
                        "不再创建匿名 new_context，改走账号持久化画像"
                    )
                runtime_lease, browser, context, _ = await self._open_browser_recovery_context(
                    "扫码登录Cookie刷新",
                    profile_key=target_account_id,
                    target_account_id=target_account_id,
                    runtime_purpose="verification_recovery",
                )
                owns_runtime_lease = runtime_lease is not None
                if context is None:
                    return False
            else:
                logger.info(f"【{target_account_id}】复用上游传入的浏览器上下文获取真实Cookie，刷新时不主动关闭该上游会话")

            if runtime_lease is None:
                logger.error(
                    f"【{target_account_id}】扫码登录Cookie刷新未获取受管 runtime_lease，"
                    "拒绝在 lease 外创建页面或注入 Cookie"
                )
                return False

            cookies = self._build_browser_cookie_payload(qr_cookies_str)

            await context.add_cookies(cookies)
            logger.info(f"【{target_account_id}】已设置 {len(cookies)} 个扫码Cookie到浏览器")

            logger.info(f"【{target_account_id}】=== 设置到浏览器的扫码Cookie ===")
            for i, cookie in enumerate(cookies, 1):
                logger.info(f"【{target_account_id}】{i:2d}. {cookie['name']}: {cookie['value'][:50]}{'...' if len(cookie['value']) > 50 else ''}")

            if page is None:
                page, _ = await account_browser_runtime_manager.get_fresh_page(runtime_lease)
            else:
                logger.info(f"【{target_account_id}】复用上游传入的页面获取真实Cookie，避免创建额外标签页")

            await asyncio.sleep(0.1)

            target_url = "https://www.goofish.com/im"
            logger.info(f"【{target_account_id}】访问页面获取真实cookie: {target_url}")

            try:
                await page.goto(target_url, wait_until='domcontentloaded', timeout=15000)
                logger.info(f"【{target_account_id}】页面访问成功")
            except Exception as e:
                if 'timeout' in str(e).lower():
                    logger.warning(f"【{target_account_id}】页面访问超时，尝试降级策略...")
                    try:
                        await page.goto(target_url, wait_until='load', timeout=20000)
                        logger.info(f"【{target_account_id}】页面访问成功（降级策略）")
                    except Exception as e2:
                        logger.warning(f"【{target_account_id}】降级策略也失败，尝试最基本访问...")
                        await page.goto(target_url, timeout=25000)
                        logger.info(f"【{target_account_id}】页面访问成功（最基本策略）")
                else:
                    raise e

            logger.info(f"【{target_account_id}】页面加载完成，等待获取真实cookie...")
            await asyncio.sleep(2)

            logger.info(f"【{target_account_id}】执行页面刷新获取最新cookie...")
            try:
                await page.reload(wait_until='domcontentloaded', timeout=12000)
                logger.info(f"【{target_account_id}】页面刷新成功")
            except Exception as e:
                if 'timeout' in str(e).lower():
                    logger.warning(f"【{target_account_id}】页面刷新超时，使用降级策略...")
                    await page.reload(wait_until='load', timeout=15000)
                    logger.info(f"【{target_account_id}】页面刷新成功（降级策略）")
                else:
                    raise e
            await asyncio.sleep(1)

            logger.info(f"【{target_account_id}】获取真实Cookie...")
            real_cookies_dict = await self._stabilize_browser_context_cookies_async(
                context,
                page,
                scene="浏览器稳定化Cookie",
                initial_cookies_dict=qr_cookies_dict,
            )

            existing_cookie = db_manager.get_cookie_details(target_account_id)
            existing_cookie_value = self._extract_cookie_value(existing_cookie)
            existing_cookies_dict = {}
            if existing_cookie_value:
                try:
                    existing_cookies_dict = trans_cookies(existing_cookie_value) or {}
                except Exception as merge_e:
                    logger.warning(f"【{target_account_id}】解析现有账号Cookie失败，按空基线继续: {self._safe_str(merge_e)}")

            merge_result = self.protected_merge_cookie_dicts(existing_cookies_dict, real_cookies_dict)
            real_cookies_dict = merge_result['merged_cookies_dict']
            if target_account_id == self._canonical_account_id():
                self._log_protected_merge_event("qr_login_protected_merge", merge_result)
            else:
                logger.info(
                    f"【{target_account_id}】qr_login_protected_merge "
                    f"incoming_count={merge_result.get('incoming_count', 0)} "
                    f"existing_count={merge_result.get('existing_count', 0)} "
                    f"merged_count={merge_result.get('merged_count', 0)} "
                    f"protected_preserved_fields={merge_result.get('preserved_protected_fields') or []} "
                    f"would_remove_fields={merge_result.get('would_remove_fields') or []} "
                    f"account_switched={merge_result.get('account_switched', False)}"
                )
            if merge_result['updated_fields']:
                logger.info(f"【{target_account_id}】扫码登录合并更新Cookie字段: {', '.join(merge_result['updated_fields'])}")
            if merge_result['preserved_fields']:
                logger.info(f"【{target_account_id}】扫码登录保留现有Cookie字段 ({len(merge_result['preserved_fields'])}个): {', '.join(merge_result['preserved_fields'])}")
            if merge_result['preserved_protected_fields']:
                logger.warning(f"【{target_account_id}】扫码登录保护性保留关键字段: {', '.join(merge_result['preserved_protected_fields'])}")
            if merge_result['account_switched']:
                logger.warning(f"【{target_account_id}】扫码登录检测到unb变化，按账号切换处理，不保留旧账号Cookie字段")

            missing_required_fields = merge_result['missing_required_fields']
            if missing_required_fields:
                if self._should_accept_business_ready_cookie_handoff(
                    real_cookies_dict,
                    missing_required_fields=missing_required_fields,
                ):
                    logger.warning(
                        f"【{target_account_id}】扫码登录真实Cookie仅缺少 cna，"
                        "但业务关键字段已齐，按 business-ready Cookie 继续保存"
                    )
                else:
                    logger.error(f"【{target_account_id}】扫码登录真实Cookie仍缺失核心字段，放弃保存: {', '.join(missing_required_fields)}")
                    return False

            real_cookies_str = '; '.join([f"{k}={v}" for k, v in real_cookies_dict.items()])

            logger.info(f"【{target_account_id}】真实Cookie已获取，包含 {len(real_cookies_dict)} 个字段")

            logger.info(f"【{target_account_id}】========== 扫码登录真实Cookie字段详情 ==========")
            logger.info(f"【{target_account_id}】Cookie字段数: {len(real_cookies_dict)}")
            logger.info(f"【{target_account_id}】Cookie字段列表:")
            for i, (key, value) in enumerate(real_cookies_dict.items(), 1):
                if len(str(value)) > 50:
                    logger.info(f"【{target_account_id}】  {i:2d}. {key}: {str(value)[:30]}...{str(value)[-20:]} (长度: {len(str(value))})")
                else:
                    logger.info(f"【{target_account_id}? {i:2d}. {key}: {value}")

            important_keys = list(REQUIRED_SESSION_COOKIE_FIELDS) + list(OBSERVED_SESSION_COOKIE_FIELDS)
            logger.info(f"【{target_account_id}】关键字段检查:")
            for key in important_keys:
                if key in real_cookies_dict:
                    val = real_cookies_dict[key]
                    logger.info(f"【{target_account_id}】  ✅ {key}: {'存在' if val else '为空'} (长度: {len(str(val)) if val else 0})")
                else:
                    logger.info(f"【{target_account_id}】  ❌ {key}: 缺失")
            logger.info(f"【{target_account_id}】==========================================")

            logger.info(f"【{target_account_id}】=== 真实Cookie摘要 ===")
            logger.info(f"【{target_account_id}】Cookie字符串长度: {len(real_cookies_str)}")
            logger.info(f"【{target_account_id}】Cookie摘要: {self._summarize_cookie_string(real_cookies_str)}")

            logger.info(f"【{target_account_id}】=== Cookie字段详细信息 ===")
            for i, (name, value) in enumerate(real_cookies_dict.items(), 1):
                if len(value) > 50:
                    display_value = f"{value[:20]}...{value[-20:]}"
                else:
                    display_value = value
                logger.info(f"【{target_account_id}】{i:2d}. {name}: {display_value}")

            logger.info(f"【{target_account_id}】=== 扫码Cookie对比 ===")
            logger.info(f"【{target_account_id}】扫码Cookie长度: {len(qr_cookies_str)}")
            logger.info(f"【{target_account_id}】扫码Cookie字段数: {len(qr_cookies_dict)}")
            logger.info(f"【{target_account_id}】真实Cookie长度: {len(real_cookies_str)}")
            logger.info(f"【{target_account_id}】真实Cookie字段数: {len(real_cookies_dict)}")
            logger.info(f"【{target_account_id}】长度增加: {len(real_cookies_str) - len(qr_cookies_str)} 字符")
            logger.info(f"【{target_account_id}】字段增加: {len(real_cookies_dict) - len(qr_cookies_dict)} 个")

            changed_cookies = []
            new_cookies = []
            for name, new_value in real_cookies_dict.items():
                old_value = qr_cookies_dict.get(name)
                if old_value is None:
                    new_cookies.append(name)
                elif old_value != new_value:
                    changed_cookies.append(name)

            if changed_cookies:
                logger.info(f"【{target_account_id}】发生变化的Cookie字段 ({len(changed_cookies)}个): {', '.join(changed_cookies)}")
            if new_cookies:
                logger.info(f"【{target_account_id}】新增的Cookie字段 ({len(new_cookies)}个): {', '.join(new_cookies)}")
            if not changed_cookies and not new_cookies:
                logger.info(f"【{target_account_id}】Cookie无变化")

            important_cookies = ['_m_h5_tk', '_m_h5_tk_enc', 'cookie2', 't', 'sgcookie', 'unb', 'uc1', 'uc3', 'uc4']
            logger.info(f"【{target_account_id}】=== 重要Cookie字段完整详情 ===")
            for cookie_name in important_cookies:
                if cookie_name in real_cookies_dict:
                    cookie_value = real_cookies_dict[cookie_name]

                    change_mark = " [已变化]" if cookie_name in changed_cookies else " [新增]" if cookie_name in new_cookies else " [无变化]"

                    logger.info(f"【{target_account_id}】{cookie_name}{change_mark}:")
                    logger.info(f"【{target_account_id}? ? {self._mask_secret_value(cookie_value, head=8, tail=6)}")
                    logger.info(f"【{target_account_id}】  长度: {len(cookie_value)}")

                    if cookie_name in qr_cookies_dict:
                        old_value = qr_cookies_dict[cookie_name]
                        if old_value != cookie_value:
                            logger.info(f"【{target_account_id}】  原值: {self._mask_secret_value(old_value, head=8, tail=6)}")
                            logger.info(f"【{target_account_id}】  原长度: {len(old_value)}")
                    logger.info(f"【{target_account_id}? ---")
                else:
                    logger.info(f"【{target_account_id}】{cookie_name}: [不存在]")

            existing_cookie = db_manager.get_cookie_details(target_account_id)
            if existing_cookie:
                success = db_manager.update_cookie_account_info(target_account_id, cookie_value=real_cookies_str)
            else:
                success = db_manager.save_cookie(target_account_id, real_cookies_str, target_user_id)

            if success:
                logger.info(f"【{target_account_id}】真实Cookie已成功保存到数据库")

                if target_account_id == self._canonical_account_id():
                    self._set_runtime_cookie_state(
                        cookies_str=real_cookies_str,
                        cookies_dict=real_cookies_dict,
                        source="qr_login_refresh",
                    )
                logger.info(f"【{target_account_id}】已更新当前实例的Cookie信息")

                self.last_qr_cookie_refresh_time = time.time()
                logger.info(f"【{target_account_id}】已更新扫码登录Cookie刷新时间标志，_refresh_cookies_via_browser将等待{self.qr_cookie_refresh_cooldown//60}分钟后执行")

                return True
            else:
                logger.error(f"【{target_account_id}】保存真实Cookie到数据库失败")
                return False

        except Exception as e:
            logger.error(f"【{target_account_id}】使用扫码cookie获取真实cookie失败: {self._safe_str(e)}")
            return False
        finally:
            try:
                if runtime_lease is not None:
                    await self._release_browser_recovery_runtime(
                        runtime_lease,
                        browser=browser,
                        context=context,
                        page=page,
                        reason="qr_cookie_refresh_completed",
                        invalidate_after_release=owns_runtime_lease,
                    )
                elif browser or context:
                    await self._async_close_browser(
                        browser=browser,
                        context=context,
                        page=page,
                        close_browser=close_browser,
                        close_context=close_context,
                        close_page=close_page,
                    )
            except Exception as cleanup_e:
                logger.warning(f"【{target_account_id}】清理浏览器资源时出错: {self._safe_str(cleanup_e)}")

    def _build_browser_cookie_payload(self, cookies_str: str) -> List[Dict[str, str]]:
        cross_domain_cookie_names = {
            't',
            'tracknick',
            'isg',
            'unb',
            'cookie2',
            '_tb_token_',
            'sgcookie',
            'csg',
            'tfstk',
            '_m_h5_tk',
            '_m_h5_tk_enc',
            'havana_lgc2_77',
            '_hvn_lgc_',
            'havana_lgc_exp',
            'mtop_partitioned_detect',
            '_samesite_flag_',
            'sdkSilent',
            'cna',
            'x5sec',
            'x5secdata',
            'XSRF-TOKEN',
            'thw',
            'cbc',
            'cnaui',
            'aui',
            'sca',
        }
        cookies: List[Dict[str, str]] = []
        seen = set()
        for cookie_pair in str(cookies_str or '').replace('\ufeff', '').split(';'):
            cookie_pair = cookie_pair.strip()
            if '=' not in cookie_pair:
                continue
            name, value = cookie_pair.split('=', 1)
            name = name.strip()
            value = value.strip()
            if not name:
                continue

            domains = ['.goofish.com']
            if name in cross_domain_cookie_names:
                domains.append('.taobao.com')

            for domain in domains:
                dedupe_key = (name, value, domain)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                cookies.append({
                    'name': name,
                    'value': value,
                    'domain': domain,
                    'path': '/',
                })
        return cookies

    def _build_browser_refresh_launch_args(self):
        browser_args = [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-accelerated-2d-canvas',
            '--no-first-run',
            '--no-zygote',
            '--disable-gpu',
            '--disable-background-timer-throttling',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding',
            '--disable-features=TranslateUI',
            '--disable-ipc-flooding-protection',
            '--disable-extensions',
            '--disable-default-apps',
            '--disable-sync',
            '--disable-translate',
            '--hide-scrollbars',
            '--mute-audio',
            '--no-default-browser-check',
            '--no-pings',
        ]

        if os.getenv('DOCKER_ENV'):
            browser_args.extend([
                '--disable-background-networking',
                '--disable-client-side-phishing-detection',
                '--disable-hang-monitor',
                '--disable-popup-blocking',
                '--disable-prompt-on-repost',
                '--disable-web-resources',
                '--metrics-recording-only',
                '--safebrowsing-disable-auto-update',
                '--password-store=basic',
                '--use-mock-keychain',
            ])

        return browser_args

    def _build_browser_refresh_context_options(self):
        return {}

    def _resolve_account_browser_profile_dir(self, profile_key: Optional[str] = None) -> str:
        resolved_key = str(profile_key or "").strip() or self._canonical_account_id()
        if not resolved_key:
            raise RuntimeError("缺少 canonical account_id，无法解析账号级浏览器画像目录")
        return account_browser_runtime_manager.resolve_profile_dir(resolved_key)

    def _should_prefer_account_persistent_profile_for_browser_recovery(self) -> Tuple[bool, str]:
        canonical_account_id = self._canonical_account_id()
        if not canonical_account_id:
            return False, "missing_canonical_account_id"

        scope_keys = []
        for candidate in (canonical_account_id,):
            resolved_candidate = str(candidate or "").strip()
            if resolved_candidate and resolved_candidate not in scope_keys:
                scope_keys.append(resolved_candidate)

        def _lookup_recovery_state(getter):
            for scope_key in scope_keys:
                try:
                    state = getter(scope_key)
                except Exception:
                    continue
                if state:
                    return state
            return None

        qr_login_grace = _lookup_recovery_state(self.get_qr_login_grace)
        if qr_login_grace:
            return True, "扫码登录缓冲期"

        manual_refresh_state = _lookup_recovery_state(self.get_manual_refresh_state)
        if isinstance(manual_refresh_state, dict):
            phase = str(manual_refresh_state.get('phase') or '').strip()
            if phase == 'handoff_recovery':
                return True, "手动刷新交接恢复窗口"
            if phase == 'manual_refresh':
                return True, "手动刷新进行中"

        try:
            if self._has_recent_slider_success():
                window_seconds = getattr(self, 'slider_success_reentry_window', None)
                if window_seconds:
                    return True, f"最近{int(window_seconds)}秒内刚过滑块"
                return True, "最近刚通过滑块"
        except Exception:
            pass

        return True, "默认复用账号持久化画像稳定浏览器恢复链路"

    async def _open_browser_recovery_context(
        self,
        recovery_label: str,
        profile_key: Optional[str] = None,
        target_account_id: Optional[str] = None,
        runtime_purpose: str = "verification_recovery",
    ) -> Tuple[Any, Any, Any, bool]:
        browser_args = self._build_browser_refresh_launch_args()
        context_options = dict(self._build_browser_refresh_context_options())
        prefer_persistent_profile, reuse_reason = (
            self._should_prefer_account_persistent_profile_for_browser_recovery()
        )
        canonical_account_id = self._canonical_account_id()
        if not canonical_account_id:
            logger.error(f"【default】{recovery_label}缺少 canonical account_id，无法申请账号级浏览器 runtime")
            return None, None, None, False

        for scope_name, scope_value in (
            ("target_account_id", target_account_id),
            ("profile_key", profile_key),
        ):
            normalized_scope = self._normalize_account_scope(scope_value)
            if normalized_scope and normalized_scope != canonical_account_id:
                logger.error(
                    f"【{canonical_account_id}】{recovery_label}拒绝跨账号浏览器恢复请求: "
                    f"{scope_name}={normalized_scope}"
                )
                return None, None, None, False

        resolved_account_id = canonical_account_id

        if prefer_persistent_profile:
            if not resolved_account_id:
                logger.error(f"【default】{recovery_label}缺少 account_id，无法申请账号级浏览器 runtime")
                return None, None, None, False
            profile_dir = account_browser_runtime_manager.resolve_profile_dir(resolved_account_id)
            persistent_context_options = dict(context_options)
            persistent_context_options.setdefault('accept_downloads', True)
            persistent_context_options.setdefault('ignore_https_errors', True)
            runtime_request = {
                "account_id": resolved_account_id,
                "purpose": runtime_purpose,
                "profile_dir": profile_dir,
                "use_persistent_context": True,
                "launch_options": {
                    "headless": True,
                    "args": browser_args,
                },
                "persistent_context_options": persistent_context_options,
            }
            try:
                lease = await account_browser_runtime_manager.acquire_runtime(
                    resolved_account_id,
                    runtime_purpose,
                    exclusive=True,
                    runtime_request=runtime_request,
                )
                lease_account_id = self._normalize_account_scope(
                    getattr(lease, "account_id", None)
                )
                if lease_account_id != resolved_account_id:
                    try:
                        await account_browser_runtime_manager.release_runtime(
                            lease,
                            reason="recovery_context_account_mismatch",
                        )
                    except Exception as release_error:
                        logger.warning(
                            f"【{resolved_account_id}】{recovery_label}释放跨账号 runtime 失败，继续按失败关闭处理: "
                            f"{self._safe_str(release_error)}"
                        )
                    logger.error(
                        f"【{resolved_account_id}】{recovery_label}拿到的账号级 runtime 归属不匹配，已放弃使用: "
                        f"lease_account_id={lease_account_id or 'default'}"
                    )
                    return None, None, None, False
                runtime = getattr(lease, "runtime", None)
                context = getattr(runtime, "context", None)
                browser = getattr(context, 'browser', None) or getattr(runtime, "browser", None)
                if context is None:
                    await account_browser_runtime_manager.release_runtime(
                        lease,
                        reason="recovery_context_missing",
                    )
                    logger.error(
                        f"【{resolved_account_id}】{recovery_label}拿到的账号级 runtime 缺少 context，已放弃使用: "
                        f"{profile_dir}"
                    )
                    return None, None, None, False
                logger.info(
                    f"【{resolved_account_id}】{recovery_label}复用账号持久化画像: {profile_dir}"
                    f"（原因: {reuse_reason}）"
                )
                return lease, browser, context, True
            except Exception as persistent_launch_error:
                logger.error(
                    f"【{resolved_account_id or 'default'}】{recovery_label}持久化画像启动失败，不再降级到匿名临时上下文: "
                    f"{self._safe_str(persistent_launch_error)}"
                )
                return None, None, None, False

        logger.error(f"【{resolved_account_id or 'default'}】{recovery_label}未命中任何账号级浏览器恢复上下文策略")
        return None, None, None, False

    async def _release_browser_recovery_runtime(
        self,
        lease,
        *,
        browser=None,
        context=None,
        page=None,
        reason: str,
        invalidate_after_release: bool = False,
        close_browser: bool = True,
        close_context: bool = True,
        close_page: bool = True,
    ):
        release_account_id = (
            str(
                getattr(lease, "account_id", None)
                or self._canonical_account_id()
                or "default"
            ).strip()
            or "default"
        )
        if lease is not None:
            try:
                await account_browser_runtime_manager.release_runtime(lease, reason=reason)
                if invalidate_after_release and release_account_id != "default":
                    try:
                        await account_browser_runtime_manager.invalidate_runtime(
                            release_account_id,
                            reason=f"{reason}_post_release_invalidate",
                        )
                    except Exception as invalidate_error:
                        logger.warning(
                            f"【{release_account_id}】释放账号级浏览器 runtime 后尝试立即失效缓存实例失败，"
                            f"将回退到 runtime manager 后续空闲回收: {self._safe_str(invalidate_error)}"
                        )
                return
            except Exception as release_error:
                logger.warning(
                    f"【{release_account_id}】释放账号级浏览器 runtime 失败，避免误关受管资源，保留 runtime manager 后续回收: "
                    f"{self._safe_str(release_error)}"
                )
                return
        if browser or context or page:
            await self._async_close_browser(
                browser=browser,
                context=context,
                page=page,
                close_browser=close_browser,
                close_context=close_context,
                close_page=close_page,
            )

    @staticmethod
    def _normalize_browser_cookie_items(cookie_items) -> Dict[str, str]:
        normalized: Dict[str, str] = {}
        for cookie in cookie_items or []:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name") or "").strip()
            if not name:
                continue
            value = cookie.get("value")
            if value is None:
                continue
            normalized[name] = str(value)
        return normalized

    async def _snapshot_browser_context_cookies_async(self, context) -> Dict[str, str]:
        if context is None:
            return {}
        cookie_reader = getattr(context, "cookies", None)
        if not callable(cookie_reader):
            return {}
        return self._normalize_browser_cookie_items(await cookie_reader())

    async def _run_browser_cookie_stabilization_action_async(
        self,
        page,
        *,
        action_name: str,
        target_url: Optional[str] = None,
        scene: str = "browser_cookie_stabilization",
    ) -> bool:
        if page is None:
            return False

        try:
            if target_url:
                logger.info(f"【{self.account_id}】{scene} 动作 {action_name} -> {target_url}")
                await page.goto(target_url, wait_until='domcontentloaded', timeout=15000)
            else:
                logger.info(f"【{self.account_id}】{scene} 动作 {action_name}")
                await page.reload(wait_until='domcontentloaded', timeout=12000)
            return True
        except Exception as action_error:
            if 'timeout' not in str(action_error).lower():
                logger.warning(f"【{self.account_id}】{scene} 动作 {action_name} 失败: {self._safe_str(action_error)}")
                return False

        try:
            if target_url:
                await page.goto(target_url, wait_until='load', timeout=20000)
            else:
                await page.reload(wait_until='load', timeout=15000)
            logger.info(f"【{self.account_id}】{scene} 动作 {action_name} 降级成功")
            return True
        except Exception as fallback_error:
            logger.warning(f"【{self.account_id}】{scene} 动作 {action_name} 降级失败: {self._safe_str(fallback_error)}")
            if target_url:
                try:
                    await page.goto(target_url, timeout=25000)
                    logger.info(f"【{self.account_id}】{scene} 动作 {action_name} 最基础访问成功")
                    return True
                except Exception as final_error:
                    logger.warning(
                        f"【{self.account_id}】{scene} 动作 {action_name} 最基础访问失败: {self._safe_str(final_error)}"
                    )
            return False

    async def _stabilize_browser_context_cookies_async(
        self,
        context,
        page=None,
        *,
        scene: str = "browser_cookie_stabilization",
        initial_cookies_dict: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        best_cookies: Dict[str, str] = {}
        try:
            best_cookies = await self._snapshot_browser_context_cookies_async(context)
        except Exception as snapshot_error:
            logger.warning(f"【{self.account_id}】{scene} 初始 Cookie 快照失败: {self._safe_str(snapshot_error)}")
            best_cookies = {}

        if not best_cookies:
            best_cookies = dict(initial_cookies_dict or {})

        best_missing = [
            key for key in REQUIRED_SESSION_COOKIE_FIELDS
            if not best_cookies.get(key)
        ]
        if not best_missing or context is None or page is None:
            return best_cookies

        async def _record_snapshot(action_name: str) -> None:
            nonlocal best_cookies, best_missing
            current_cookies = await self._snapshot_browser_context_cookies_async(context)
            current_missing = [
                key for key in REQUIRED_SESSION_COOKIE_FIELDS
                if not current_cookies.get(key)
            ]
            logger.info(
                f"【{self.account_id}】{scene} 快照[{action_name}] "
                f"field_count={len(current_cookies)} missing_required_fields={current_missing}"
            )
            if current_cookies and len(current_missing) < len(best_missing):
                best_cookies = current_cookies
                best_missing = current_missing

        async def _settle_page(work_page, action_name: str, target_url: Optional[str]) -> None:
            action_ok = await self._run_browser_cookie_stabilization_action_async(
                work_page,
                action_name=action_name,
                target_url=target_url,
                scene=scene,
            )
            if not action_ok:
                return

            await asyncio.sleep(1.0)
            wait_for_load_state = getattr(work_page, "wait_for_load_state", None)
            if callable(wait_for_load_state):
                try:
                    await wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
            await asyncio.sleep(0.5)
            await _record_snapshot(action_name)

        for action_name, target_url in (
            ("reload_current", None),
            ("goto_home", "https://www.goofish.com/"),
            ("goto_im", "https://www.goofish.com/im"),
        ):
            await _settle_page(page, action_name, target_url)
            if not best_missing:
                return best_cookies

        fresh_page = None
        new_page_factory = getattr(context, "new_page", None)
        if callable(new_page_factory):
            try:
                fresh_page = await new_page_factory()
                for action_name, target_url in (
                    ("fresh_tab_home", "https://www.goofish.com/"),
                    ("fresh_tab_im", "https://www.goofish.com/im"),
                ):
                    await _settle_page(fresh_page, action_name, target_url)
                    if not best_missing:
                        break
            except Exception as fresh_page_error:
                logger.warning(f"【{self.account_id}】{scene} fresh-tab 预热失败: {self._safe_str(fresh_page_error)}")
            finally:
                if fresh_page is not None:
                    close_page = getattr(fresh_page, "close", None)
                    if callable(close_page):
                        try:
                            await close_page()
                        except Exception:
                            pass

        return best_cookies

    async def _refresh_cookies_via_browser_page(self, current_cookies_str: str, restart_on_success: bool = True):
        browser = None
        context = None
        page = None
        runtime_lease = None
        canonical_account_id = self._canonical_account_id()
        if not canonical_account_id:
            logger.error("【default】浏览器稳定化缺少 canonical account_id，拒绝继续执行账号级浏览器链路")
            return False
        log_account_id = canonical_account_id or "default"

        try:
            from utils.xianyu_utils import trans_cookies

            logger.info(f"【{log_account_id}】开始使用当前cookie访问指定页面获取真实cookie...")
            logger.info(f"【{log_account_id}】当前cookie长度: {len(current_cookies_str)}")

            current_cookies_dict = trans_cookies(current_cookies_str)
            logger.info(f"【{log_account_id}】当前cookie字段数: {len(current_cookies_dict)}")
            runtime_lease, browser, context, _ = await self._open_browser_recovery_context(
                "浏览器稳定化",
                runtime_purpose="cookie_refresh",
            )
            if context is None:
                return False

            cookies = self._build_browser_cookie_payload(current_cookies_str)

            await context.add_cookies(cookies)
            logger.info(f"【{log_account_id}】已设置 {len(cookies)} 个当前Cookie到浏览器")

            page, _ = await account_browser_runtime_manager.get_fresh_page(runtime_lease)

            await asyncio.sleep(0.1)

            target_url = "https://www.goofish.com/im"
            logger.info(f"【{log_account_id}】访问页面获取真实cookie: {target_url}")

            try:
                await page.goto(target_url, wait_until='domcontentloaded', timeout=15000)
                logger.info(f"【{log_account_id}】页面访问成功")
            except Exception as e:
                if 'timeout' in str(e).lower():
                    logger.warning(f"【{log_account_id}】页面访问超时，尝试降级策略...")
                    try:
                        await page.goto(target_url, wait_until='load', timeout=20000)
                        logger.info(f"【{log_account_id}】页面访问成功（降级策略）")
                    except Exception as e2:
                        logger.warning(f"【{log_account_id}】降级策略也失败，尝试最基本访问...")
                        await page.goto(target_url, timeout=25000)
                        logger.info(f"【{log_account_id}】页面访问成功（最基本策略）")
                else:
                    raise e

            logger.info(f"【{log_account_id}】页面加载完成，等待获取真实cookie...")
            await asyncio.sleep(2)

            logger.info(f"【{log_account_id}】执行页面刷新获取最新cookie...")
            try:
                await page.reload(wait_until='domcontentloaded', timeout=12000)
                logger.info(f"【{log_account_id}】页面刷新成功")
            except Exception as e:
                if 'timeout' in str(e).lower():
                    logger.warning(f"【{log_account_id}】页面刷新超时，使用降级策略...")
                    await page.reload(wait_until='load', timeout=15000)
                    logger.info(f"【{log_account_id}】页面刷新成功（降级策略）")
                else:
                    raise e
            await asyncio.sleep(1)

            logger.info(f"【{log_account_id}】获取真实Cookie...")
            real_cookies_dict = await self._stabilize_browser_context_cookies_async(
                context,
                page,
                scene="扫码登录Cookie刷新",
                initial_cookies_dict=current_cookies_dict,
            )

            merge_result = self.protected_merge_cookie_dicts(current_cookies_dict, real_cookies_dict)
            real_cookies_dict = merge_result['merged_cookies_dict']
            self._log_protected_merge_event("browser_stabilization_protected_merge", merge_result)

            real_cookies_str = '; '.join([f"{k}={v}" for k, v in real_cookies_dict.items()])

            logger.info(f"【{log_account_id}】真实Cookie已获取，包含 {len(real_cookies_dict)} 个字段")
            logger.info(f"【{log_account_id}】真实Cookie摘要: {self._summarize_cookie_string(real_cookies_str)}")

            self._log_cookie_merge_summary(
                real_cookies_dict,
                merge_result['updated_fields'],
                merge_result['changed_fields'],
                merge_result['new_fields'],
                context="浏览器稳定化Cookie",
                preserved_fields=merge_result['preserved_fields'],
                preserved_protected_fields=merge_result['preserved_protected_fields'],
                would_remove_fields=merge_result['would_remove_fields'],
                removed_fields=merge_result['removed_fields'],
                missing_protected_fields=merge_result['missing_protected_fields'],
                missing_required_fields=merge_result['missing_required_fields'],
                incoming_missing_protected_fields=merge_result['incoming_missing_protected_fields'],
                account_switched=merge_result['account_switched'],
            )

            if merge_result['missing_required_fields']:
                logger.error(f"【{log_account_id}】浏览器稳定化后的Cookie仍缺失核心字段，放弃写回数据库: {', '.join(merge_result['missing_required_fields'])}")
                return False

            changed_cookies = []
            new_cookies = []
            for name, new_value in real_cookies_dict.items():
                old_value = current_cookies_dict.get(name)
                if old_value is None:
                    new_cookies.append(name)
                elif old_value != new_value:
                    changed_cookies.append(name)

            if not changed_cookies and not new_cookies:
                if restart_on_success:
                    logger.warning(f"【{log_account_id}】Cookie无变化，可能当前cookie已失效")
                    return False
                logger.info(f"【{log_account_id}】Cookie字段无变化，但浏览器稳定化访问已完成")

            logger.info(f"【{log_account_id}】发生变化的Cookie字段 ({len(changed_cookies)}个): {', '.join(changed_cookies[:10])}")
            if new_cookies:
                logger.info(f"【{log_account_id}】新增的Cookie字段 ({len(new_cookies)}个): {', '.join(new_cookies[:10])}")

            if restart_on_success:
                logger.info(f"【{log_account_id}】开始更新Cookie并重启任务...")
                update_success = await self._update_cookies_and_restart(real_cookies_str)

                if update_success:
                    logger.info(f"【{log_account_id}】通过访问指定页面成功更新Cookie并重启任务")
                    return True
                else:
                    logger.error(f"【{log_account_id}】更新Cookie或重启任务失败")
                    return False

            old_cookies_str = self.cookies_str
            old_cookies_dict = self.cookies.copy()
            try:
                self._set_runtime_cookie_state(
                    cookies_str=real_cookies_str,
                    cookies_dict=real_cookies_dict,
                    source="stabilize_cookie_snapshot",
                )
                await self.update_config_cookies()
                logger.info(f"【{log_account_id}】通过访问指定页面成功稳定当前Cookie（不重启任务）")
                return True
            except Exception as update_e:
                self._set_runtime_cookie_state(
                    cookies_str=old_cookies_str,
                    cookies_dict=old_cookies_dict,
                    source="stabilize_cookie_snapshot_rollback",
                )
                logger.error(f"【{log_account_id}】稳定Cookie时更新数据库失败: {self._safe_str(update_e)}")
                return False

        except Exception as e:
            logger.error(f"【{log_account_id}】使用当前cookie访问指定页面获取真实cookie失败: {self._safe_str(e)}")
            return False
        finally:
            try:
                if runtime_lease is not None or browser or context or page:
                    await self._release_browser_recovery_runtime(
                        runtime_lease,
                        browser=browser,
                        context=context,
                        page=page,
                        reason="browser_stabilization_completed",
                        invalidate_after_release=True,
                    )
            except Exception as cleanup_e:
                logger.warning(f"【{log_account_id}】清理浏览器资源时出错: {self._safe_str(cleanup_e)}")

    def reset_qr_cookie_refresh_flag(self):
        self.last_qr_cookie_refresh_time = 0
        logger.info(f"【{self.account_id}】已重置扫码登录Cookie刷新标志")

    def get_qr_cookie_refresh_remaining_time(self) -> int:
        current_time = time.time()
        time_since_qr_refresh = current_time - self.last_qr_cookie_refresh_time
        remaining_time = max(0, self.qr_cookie_refresh_cooldown - time_since_qr_refresh)
        return int(remaining_time)

    async def _refresh_cookies_via_browser(self, triggered_by_refresh_token: bool = False):


        browser = None
        context = None
        page = None
        runtime_lease = None
        canonical_account_id = self._canonical_account_id()
        if not canonical_account_id:
            logger.error("【default】浏览器刷新Cookie缺少 canonical account_id，拒绝继续执行账号级浏览器链路")
            return False
        log_account_id = canonical_account_id or "default"
        try:

            current_time = time.time()
            time_since_qr_refresh = current_time - self.last_qr_cookie_refresh_time

            if time_since_qr_refresh < self.qr_cookie_refresh_cooldown:
                remaining_time = self.qr_cookie_refresh_cooldown - time_since_qr_refresh
                remaining_minutes = int(remaining_time // 60)
                remaining_seconds = int(remaining_time % 60)

                logger.info(f"【{log_account_id}】扫码登录Cookie刷新冷却中，还需等待 {remaining_minutes}分{remaining_seconds}秒")
                logger.info(f"【{log_account_id}】跳过本次浏览器Cookie刷新")
                return False

            logger.info(f"【{log_account_id}】开始通过浏览器刷新Cookie...")
            logger.info(f"【{log_account_id}】刷新前Cookie长度: {len(self.cookies_str)}")
            logger.info(f"【{log_account_id}】刷新前Cookie字段数: {len(self.cookies)}")
            runtime_lease, browser, context, _ = await self._open_browser_recovery_context(
                "浏览器刷新Cookie",
                runtime_purpose="cookie_refresh",
            )
            if context is None:
                return False

            cookies = self._build_browser_cookie_payload(self.cookies_str)

            await context.add_cookies(cookies)
            logger.info(f"【{log_account_id}】已设置 {len(cookies)} 个Cookie到浏览器")

            page, _ = await account_browser_runtime_manager.get_fresh_page(runtime_lease)

            await asyncio.sleep(0.1)

            target_url = "https://www.goofish.com/im"
            logger.info(f"【{log_account_id}】访问页面: {target_url}")

            try:
                await page.goto(target_url, wait_until='domcontentloaded', timeout=15000)
                logger.info(f"【{log_account_id}】页面访问成功")
            except Exception as e:
                if 'timeout' in str(e).lower():
                    logger.warning(f"【{log_account_id}】页面访问超时，尝试降级策略...")
                    try:
                        await page.goto(target_url, wait_until='load', timeout=20000)
                        logger.info(f"【{log_account_id}】页面访问成功（降级策略）")
                    except Exception as e2:
                        logger.warning(f"【{log_account_id}】降级策略也失败，尝试最基本访问...")
                        await page.goto(target_url, timeout=25000)
                        logger.info(f"【{log_account_id}】页面访问成功（最基本策略）")
                else:
                    raise e

            logger.info(f"【{log_account_id}】页面加载完成，开始刷新...")
            await asyncio.sleep(1)

            logger.info(f"【{log_account_id}】执行第一次刷新...")
            try:
                await page.reload(wait_until='domcontentloaded', timeout=12000)
                logger.info(f"【{log_account_id}】第一次刷新成功")
            except Exception as e:
                if 'timeout' in str(e).lower():
                    logger.warning(f"【{log_account_id}】第一次刷新超时，使用降级策略...")
                    await page.reload(wait_until='load', timeout=15000)
                    logger.info(f"【{log_account_id}】第一次刷新成功（降级策略）")
                else:
                    raise e
            await asyncio.sleep(1)

            logger.info(f"【{log_account_id}】执行第二次刷新...")
            try:
                await page.reload(wait_until='domcontentloaded', timeout=12000)
                logger.info(f"【{log_account_id}】第二次刷新成功")
            except Exception as e:
                if 'timeout' in str(e).lower():
                    logger.warning(f"【{log_account_id}】第二次刷新超时，使用降级策略...")
                    await page.reload(wait_until='load', timeout=15000)
                    logger.info(f"【{log_account_id}】第二次刷新成功（降级策略）")
                else:
                    raise e
            await asyncio.sleep(1)

            logger.info(f"【{log_account_id}】获取更新后的Cookie...")
            updated_cookies = await context.cookies()

            page_title = await page.title()
            logger.info(f"【{log_account_id}】当前页面标题: {page_title}")

            new_cookies_dict = {}
            for cookie in updated_cookies:
                new_cookies_dict[cookie['name']] = cookie['value']

            changed_cookies = []
            new_cookies = []
            for name, new_value in new_cookies_dict.items():
                old_value = self.cookies.get(name)
                if old_value is None:
                    new_cookies.append(name)
                elif old_value != new_value:
                    changed_cookies.append(name)

            merge_result = self.protected_merge_cookie_dicts(self.cookies, new_cookies_dict)
            merged_cookies_dict = merge_result['merged_cookies_dict']
            self._log_protected_merge_event("browser_refresh_protected_merge", merge_result)

            self._log_cookie_merge_summary(
                merged_cookies_dict,
                merge_result['updated_fields'],
                merge_result['changed_fields'],
                merge_result['new_fields'],
                context="浏览器刷新Cookie",
                preserved_fields=merge_result['preserved_fields'],
                preserved_protected_fields=merge_result['preserved_protected_fields'],
                would_remove_fields=merge_result['would_remove_fields'],
                removed_fields=merge_result['removed_fields'],
                missing_protected_fields=merge_result['missing_protected_fields'],
                missing_required_fields=merge_result['missing_required_fields'],
                incoming_missing_protected_fields=merge_result['incoming_missing_protected_fields'],
                account_switched=merge_result['account_switched'],
            )

            if merge_result['missing_required_fields']:
                logger.error(
                    f"【{log_account_id}】浏览器刷新后的Cookie仍缺失核心字段，放弃覆盖当前Cookie: {', '.join(merge_result['missing_required_fields'])}"
                )
                return False

            old_cookies_str = self.cookies_str
            old_cookies_dict = self.cookies.copy()
            self._set_runtime_cookie_state(
                cookies_dict=merged_cookies_dict,
                source="browser_cookie_refresh",
            )

            logger.info(f"【{log_account_id}】Cookie已更新，包含 {len(new_cookies_dict)} 个字段")

            if changed_cookies:
                logger.info(f"【{log_account_id}】发生变化的Cookie字段 ({len(changed_cookies)}个): {', '.join(changed_cookies)}")
            if new_cookies:
                logger.info(f"【{log_account_id}】新增的Cookie字段 ({len(new_cookies)}个): {', '.join(new_cookies)}")
            if not changed_cookies and not new_cookies:
                logger.info(f"【{log_account_id}】Cookie无变化")

            logger.info(f"【{log_account_id}】更新后的Cookie摘要: {self._summarize_cookie_string(self.cookies_str)}")

            important_cookies = ['_m_h5_tk', '_m_h5_tk_enc', 'cookie2', 't', 'sgcookie', 'unb', 'uc1', 'uc3', 'uc4']
            logger.info(f"【{log_account_id}】重要Cookie字段详情:")
            for cookie_name in important_cookies:
                if cookie_name in new_cookies_dict:
                    cookie_value = new_cookies_dict[cookie_name]
                    if len(cookie_value) > 20:
                        display_value = f"{cookie_value[:8]}...{cookie_value[-8:]}"
                    else:
                        display_value = cookie_value

                    change_mark = " [已变化]" if cookie_name in changed_cookies else " [新增]" if cookie_name in new_cookies else ""
                    logger.info(f"【{log_account_id}? {cookie_name}: {display_value}{change_mark}")

            try:
                await self.update_config_cookies()
            except Exception as update_e:
                self._set_runtime_cookie_state(
                    cookies_str=old_cookies_str,
                    cookies_dict=old_cookies_dict,
                    source="browser_cookie_refresh_rollback",
                )
                logger.error(f"【{log_account_id}】浏览器刷新Cookie写库失败，已回滚运行态Cookie: {self._safe_str(update_e)}")
                return False

            if triggered_by_refresh_token:
                self.browser_cookie_refreshed = True
                logger.info(f"【{log_account_id}】由refresh_token触发，浏览器Cookie刷新成功标志已设置为True")

                try:
                    self.restarted_in_browser_refresh = True

                    logger.info(f"【{log_account_id}】Cookie刷新成功，准备重启实例...(via _refresh_cookies_via_browser)")
                    await self._restart_instance()

                    logger.info(f"【{log_account_id}】重启请求已触发(via _refresh_cookies_via_browser)")

                    self.connection_restart_flag = True
                except Exception as e:
                    logger.error(f"【{log_account_id}】兜底重启失败: {self._safe_str(e)}")
            else:
                logger.info(f"【{log_account_id}】由定时任务触发，不设置浏览器Cookie刷新成功标志")

            logger.info(f"【{log_account_id}】Cookie刷新完成")
            return True

        except Exception as e:
            logger.error(f"【{log_account_id}】过浏览器刷新Cookie失败: {self._safe_str(e)}")
            return False
        finally:
            try:
                if runtime_lease is not None or browser or context or page:
                    await self._release_browser_recovery_runtime(
                        runtime_lease,
                        browser=browser,
                        context=context,
                        page=page,
                        reason="browser_cookie_refresh_completed",
                        invalidate_after_release=True,
                    )
            except Exception as cleanup_e:
                logger.warning(f"【{log_account_id}】创建浏览器关闭任务时出错: {self._safe_str(cleanup_e)}")

    async def _async_close_browser(
        self,
        browser,
        context=None,
        page=None,
        close_browser=True,
        close_context=True,
        close_page=True,
    ):
        try:
            logger.info(f"【{self.account_id}】开始异步关闭浏览器...")
            await asyncio.wait_for(
                self._normal_close_resources(
                    browser=browser,
                    context=context,
                    page=page,
                    close_browser=close_browser,
                    close_context=close_context,
                    close_page=close_page,
                ),
                timeout=10.0
            )
            if close_browser and browser and (context is not None or page is not None):
                try:
                    await asyncio.wait_for(browser.close(), timeout=5.0)
                    logger.info(f"【{self.account_id}】浏览器关闭完成")
                except asyncio.TimeoutError:
                    logger.warning(f"【{self.account_id}】浏览器关闭超时，尝试强制关闭")
                    try:
                        if hasattr(browser, '_connection'):
                            browser._connection.dispose()
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning(f"【{self.account_id}】关闭浏览器时出错: {self._safe_str(e)}")
            logger.info(f"【{self.account_id}】浏览器正常关闭完成")
        except asyncio.TimeoutError:
            logger.warning(f"【{self.account_id}】正常关闭超时，开始强制关闭...")
            await self._force_close_resources(
                browser=browser,
                context=context,
                page=page,
                close_browser=close_browser,
                close_context=close_context,
                close_page=close_page,
            )
        except Exception as e:
            logger.warning(f"【{self.account_id}】异步关闭时出错，强制关闭: {self._safe_str(e)}")
            await self._force_close_resources(
                browser=browser,
                context=context,
                page=page,
                close_browser=close_browser,
                close_context=close_context,
                close_page=close_page,
            )

    async def _normal_close_resources(
        self,
        browser,
        context=None,
        page=None,
        close_browser=True,
        close_context=True,
        close_page=True,
    ):
        try:
            if close_page and page:
                try:
                    await asyncio.wait_for(page.close(), timeout=5.0)
                    logger.info(f"【{self.account_id}】页面关闭完成")
                except asyncio.TimeoutError:
                    logger.warning(f"【{self.account_id}】页面关闭超时，尝试强制关闭")
                    try:
                        if hasattr(page, '_connection'):
                            page._connection.dispose()
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning(f"【{self.account_id}】关闭页面时出错: {self._safe_str(e)}")

            if close_context and context:
                try:
                    await asyncio.wait_for(context.close(), timeout=5.0)
                    logger.info(f"【{self.account_id}】浏览器上下文关闭完成")
                except asyncio.TimeoutError:
                    logger.warning(f"【{self.account_id}】浏览器上下文关闭超时，尝试强制关闭")
                    try:
                        if hasattr(context, '_connection'):
                            context._connection.dispose()
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning(f"【{self.account_id}】关闭浏览器上下文时出错: {self._safe_str(e)}")
            elif close_browser and browser and context is None and page is None:
                try:
                    await asyncio.wait_for(browser.close(), timeout=5.0)
                    logger.info(f"【{self.account_id}】浏览器关闭完成")
                except asyncio.TimeoutError:
                    logger.warning(f"【{self.account_id}】浏览器关闭超时，尝试强制关闭")
                    try:
                        if hasattr(browser, '_connection'):
                            browser._connection.dispose()
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning(f"【{self.account_id}】关闭浏览器时出错: {self._safe_str(e)}")
        except Exception as e:
            logger.error(f"【{self.account_id}】正常关闭时出现异常: {self._safe_str(e)}")
            raise

    async def _force_close_resources(
        self,
        browser,
        context=None,
        page=None,
        close_browser=True,
        close_context=True,
        close_page=True,
    ):
        try:
            logger.warning(f"【{self.account_id}】开始强制关闭资源...")
            resources = []
            if close_page and page:
                resources.append(("页面", page))
            if close_context and context:
                resources.append(("浏览器上下文", context))
            if close_browser and browser:
                resources.append(("浏览器", browser))

            if not resources:
                logger.info(f"【{self.account_id}】没有需要强制关闭的资源")
                return

            for resource_name, resource in resources:
                try:
                    await asyncio.wait_for(resource.close(), timeout=3.0)
                except Exception:
                    logger.warning(f"【{self.account_id}】{resource_name}强制关闭失败，尝试直接清理连接")
                    try:
                        if hasattr(resource, '_connection'):
                            resource._connection.dispose()
                    except Exception:
                        pass

            logger.info(f"【{self.account_id}】强制关闭完成")
        except Exception as e:
            logger.warning(f"【{self.account_id}】强制关闭时出现异常（已忽略）: {self._safe_str(e)}")

    async def send_msg_once(self, toid, item_id, text):
        headers = self._build_websocket_headers()

        logger.info(f"【{self.account_id}】开始单次发送消息: toid={toid}, item_id={item_id}")

        try:
            async with websockets.connect(
                self.base_url,
                extra_headers=headers,
                open_timeout=self.websocket_open_timeout,
                close_timeout=5
            ) as websocket:
                result = await self._handle_websocket_connection(websocket, toid, item_id, text)
                if result:
                    logger.info(f"【{self.account_id}】单次发送消息成功")
                else:
                    raise Exception("消息发送失败")
        except TypeError as e:
            error_msg = self._safe_str(e)

            if "extra_headers" in error_msg:
                logger.warning("websockets库不支持extra_headers参数，使用兼容模式")
                async with websockets.connect(
                    self.base_url,
                    additional_headers=headers,
                    open_timeout=self.websocket_open_timeout,
                    close_timeout=5
                ) as websocket:
                    result = await self._handle_websocket_connection(websocket, toid, item_id, text)
                    if result:
                        logger.info(f"【{self.account_id}】单次发送消息成功(兼容模式)")
                    else:
                        raise Exception("消息发送失败")
            else:
                raise
        except websockets.exceptions.ConnectionClosedError as e:
            logger.warning(f"【{self.account_id}】WebSocket连接关闭: {self._safe_str(e)}")
        except Exception as e:
            logger.error(f"【{self.account_id}】处理WebSocket消息异常: {self._safe_str(e)}")
            raise

    async def send_delivery_steps_once(self, toid, item_id, delivery_steps):
        headers = self._build_websocket_headers()

        logger.info(f"【{self.account_id}】开始单次发送发货步骤: toid={toid}, item_id={item_id}, steps={len(delivery_steps or [])}")

        try:
            async with websockets.connect(
                self.base_url,
                extra_headers=headers,
                open_timeout=self.websocket_open_timeout,
                close_timeout=5
            ) as websocket:
                result = await self._handle_websocket_connection_steps(websocket, toid, item_id, delivery_steps)
                if result:
                    logger.info(f"【{self.account_id}】单次发送发货步骤成功")
                else:
                    raise Exception("发货步骤发送失败")
        except TypeError as e:
            error_msg = self._safe_str(e)

            if "extra_headers" in error_msg:
                logger.warning("websockets库不支持extra_headers参数，使用兼容模式发送发货步骤")
                async with websockets.connect(
                    self.base_url,
                    additional_headers=headers,
                    open_timeout=self.websocket_open_timeout,
                    close_timeout=5
                ) as websocket:
                    result = await self._handle_websocket_connection_steps(websocket, toid, item_id, delivery_steps)
                    if result:
                        logger.info(f"【{self.account_id}】单次发送发货步骤成功(兼容模式)")
                    else:
                        raise Exception("发货步骤发送失败")
            else:
                raise
        except websockets.exceptions.ConnectionClosedError as e:
            logger.warning(f"【{self.account_id}】WebSocket连接关闭: {self._safe_str(e)}")
        except Exception as e:
            logger.error(f"【{self.account_id}】单次发送发货步骤异常: {self._safe_str(e)}")
            raise

    async def _handle_websocket_connection_steps(self, websocket, toid, item_id, delivery_steps):
        try:
            await self.init(websocket)
            await self.create_chat(websocket, toid, item_id)

            timeout = 30
            start_time = time.time()

            async for message in websocket:
                try:
                    if time.time() - start_time > timeout:
                        logger.warning(f"【{self.account_id}】WebSocket消息等待超时")
                        break

                    logger.info(f"【{self.account_id}】message: {message}")
                    message = json.loads(message)
                    cid = message["body"]["singleChatConversation"]["cid"]
                    cid = cid.split('@')[0]
                    await self._send_delivery_steps(
                        websocket,
                        cid,
                        toid,
                        delivery_steps,
                        log_prefix="单次手动发货"
                    )
                    logger.info(f'【{self.account_id}】send delivery steps success')
                    return True
                except KeyError:
                    continue
                except Exception as e:
                    logger.warning(f"【{self.account_id}】处理消息异常: {self._safe_str(e)}")
                    continue

            logger.warning(f"【{self.account_id}】WebSocket连接关闭，未能发送发货步骤")
            return False
        except Exception as e:
            logger.error(f"【{self.account_id}】WebSocket发货步骤处理异常: {self._safe_str(e)}")
            return False

    async def _create_websocket_connection(self, headers):
        import websockets

        websockets_version = getattr(websockets, '__version__', '未知')
        logger.info(f"【{self.account_id}】websockets库版本: {websockets_version}, open_timeout={self.websocket_open_timeout}s")

        proxy_url = self._get_proxy_url()
        proxy_sock = None

        if proxy_url:
            proxy_type = self.proxy_config.get('proxy_type', 'none')
            logger.info(f"【{self.account_id}】WebSocket将过代理连接: {proxy_type}://{self.proxy_config.get('proxy_host')}:{self.proxy_config.get('proxy_port')}")

            try:
                from python_socks.async_.asyncio.v2 import Proxy
                from python_socks import ProxyType as SocksProxyType
                import ssl

                if proxy_type == 'socks5':
                    socks_type = SocksProxyType.SOCKS5
                elif proxy_type == 'socks4':
                    socks_type = SocksProxyType.SOCKS4
                elif proxy_type in ['http', 'https']:
                    socks_type = SocksProxyType.HTTP
                else:
                    socks_type = None

                if socks_type:
                    parsed_url = urllib.parse.urlparse(self.base_url)
                    dest_host = parsed_url.hostname
                    dest_port = parsed_url.port or (443 if parsed_url.scheme == 'wss' else 80)

                    proxy = Proxy(
                        proxy_type=socks_type,
                        host=self.proxy_config.get('proxy_host'),
                        port=self.proxy_config.get('proxy_port'),
                        username=self.proxy_config.get('proxy_user') or None,
                        password=self.proxy_config.get('proxy_pass') or None
                    )

                    proxy_sock = await proxy.connect(
                        dest_host=dest_host,
                        dest_port=dest_port
                    )

                    if parsed_url.scheme == 'wss':
                        ssl_context = ssl.create_default_context()
                        proxy_sock = ssl_context.wrap_socket(
                            proxy_sock,
                            server_hostname=dest_host
                        )

                    logger.info(f"【{self.account_id}】代理连接建立成功")

            except ImportError as e:
                logger.warning(f"【{self.account_id}】代理连接需要安装 python-socks: pip install python-socks[asyncio]")
                logger.warning(f"【{self.account_id}】将尝试不使用代理进行WebSocket连接")
                proxy_sock = None
            except Exception as e:
                logger.error(f"【{self.account_id}】过代理建立连接失败: {self._safe_str(e)}")
                logger.warning(f"【{self.account_id}】将尝试不使用代理进行WebSocket连接")
                proxy_sock = None

        try:
            connect_kwargs = {
                'extra_headers': headers,
                'open_timeout': self.websocket_open_timeout,
            }
            if proxy_sock:
                connect_kwargs['sock'] = proxy_sock

            return websockets.connect(
                self.base_url,
                **connect_kwargs
            )
        except Exception as e:
            error_msg = self._safe_str(e)
            logger.warning(f"【{self.account_id}】extra_headers参数失败: {error_msg}")

            if "extra_headers" in error_msg or "unexpected keyword argument" in error_msg:
                logger.warning(f"【{self.account_id}】websockets库不支持extra_headers参数，尝试additional_headers")
                try:
                    connect_kwargs = {
                        'additional_headers': headers,
                        'open_timeout': self.websocket_open_timeout,
                    }
                    if proxy_sock:
                        connect_kwargs['sock'] = proxy_sock

                    return websockets.connect(
                        self.base_url,
                        **connect_kwargs
                    )
                except Exception as e2:
                    error_msg2 = self._safe_str(e2)
                    logger.warning(f"【{self.account_id}】additional_headers参数失败: {error_msg2}")

                    if "additional_headers" in error_msg2 or "unexpected keyword argument" in error_msg2:
                        raise RuntimeError(
                            f"当前websockets库不支持header参数，无法安全建立鉴权连接: {error_msg2}"
                        )
                    else:
                        raise e2
            else:
                raise e

    async def _handle_websocket_connection(self, websocket, toid, item_id, text):
        try:
            await self.init(websocket)
            await self.create_chat(websocket, toid, item_id)

            timeout = 30
            start_time = time.time()

            async for message in websocket:
                try:
                    if time.time() - start_time > timeout:
                        logger.warning(f"【{self.account_id}】WebSocket消息等待超时")
                        break

                    logger.info(f"【{self.account_id}】message: {message}")
                    message = json.loads(message)
                    cid = message["body"]["singleChatConversation"]["cid"]
                    cid = cid.split('@')[0]
                    await self.send_msg(websocket, cid, toid, text)
                    logger.info(f'【{self.account_id}】send message success')
                    return True
                except KeyError:
                    continue
                except Exception as e:
                    logger.warning(f"【{self.account_id}】处理消息异常: {self._safe_str(e)}")
                    continue

            logger.warning(f"【{self.account_id}】WebSocket连接关闭，未能发送消息")
            return False
        except Exception as e:
            logger.error(f"【{self.account_id}】WebSocket连接处理异常: {self._safe_str(e)}")
            return False

    def is_chat_message(self, message):
        try:
            return (
                isinstance(message, dict)
                and "1" in message
                and isinstance(message["1"], dict)
                and "10" in message["1"]
                and isinstance(message["1"]["10"], dict)
                and "reminderContent" in message["1"]["10"]
            )
        except Exception:
            return False

    def is_sync_package(self, message_data):
        try:
            return (
                isinstance(message_data, dict)
                and "body" in message_data
                and "syncPushPackage" in message_data["body"]
                and "data" in message_data["body"]["syncPushPackage"]
                and len(message_data["body"]["syncPushPackage"]["data"]) > 0
            )
        except Exception:
            return False

    async def create_session(self):
        if not self.session:
            headers = DEFAULT_HEADERS.copy()

            proxy_url = self._get_proxy_url()
            connector = None

            if proxy_url:
                proxy_type = self.proxy_config.get('proxy_type', 'none')
                logger.info(f"【{self.account_id}】创建带代理的Session: {proxy_type}://{self.proxy_config.get('proxy_host')}:{self.proxy_config.get('proxy_port')}")

                if proxy_type == 'socks5':
                    try:
                        from aiohttp_socks import ProxyConnector, ProxyType
                        connector = ProxyConnector(
                            proxy_type=ProxyType.SOCKS5,
                            host=self.proxy_config.get('proxy_host'),
                            port=self.proxy_config.get('proxy_port'),
                            username=self.proxy_config.get('proxy_user') or None,
                            password=self.proxy_config.get('proxy_pass') or None,
                            rdns=True
                        )
                    except ImportError:
                        logger.error(f"【{self.account_id}】SOCKS5代理需要安装 aiohttp-socks: pip install aiohttp-socks")
                        connector = None
                else:
                    connector = aiohttp.TCPConnector(limit=100, limit_per_host=30)
            else:
                connector = aiohttp.TCPConnector(limit=100, limit_per_host=30)

            self.session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
                connector=connector
            )
            self._sync_session_cookie_header()

            self._http_proxy_url = proxy_url if proxy_url and self.proxy_config.get('proxy_type') in ['http', 'https'] else None

    async def close_session(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def get_api_reply(self, msg_time, user_url, send_user_id, send_user_name, item_id, send_message, chat_id):
        try:
            if not self.session:
                await self.create_session()

            api_config = AUTO_REPLY.get('api', {})
            timeout = aiohttp.ClientTimeout(total=api_config.get('timeout', 10))
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error("【default】API 回复缺少 canonical account_id，拒绝继续运行")
                return None

            payload = {
                "account_id": current_account_id,
                "msg_time": msg_time,
                "user_url": user_url,
                "send_user_id": send_user_id,
                "send_user_name": send_user_name,
                "item_id": item_id,
                "send_message": send_message,
                "chat_id": chat_id
            }

            async with self.session.post(
                api_config.get('url', 'http://localhost:8080/xianyu/reply'),
                json=payload,
                timeout=timeout
            ) as response:
                result = await response.json()

                if str(result.get('code')) == '200' or result.get('code') == 200:
                    send_msg = result.get('data', {}).get('send_msg')
                    if send_msg:
                        return send_msg.format(
                            send_user_id=payload['send_user_id'],
                            send_user_name=payload['send_user_name'],
                            send_message=payload['send_message']
                        )
                    else:
                        logger.warning("API返回成功但无回复消息")
                        return None
                else:
                    logger.warning(f"API返回错误: {result.get('msg', '未知错误')}")
                    return None

        except asyncio.TimeoutError:
            logger.error("API调用超时")
            return None
        except Exception as e:
            logger.error(f"调用API出错: {self._safe_str(e)}")
            return None

    async def _handle_message_with_semaphore(self, message_data, websocket, msg_id="unknown"):
        async with self.message_semaphore:
            self.active_message_tasks += 1
            try:
                await self.handle_message(message_data, websocket, msg_id)
            finally:
                self.active_message_tasks -= 1
                if self.active_message_tasks % 100 == 0 and self.active_message_tasks > 0:
                    logger.info(f"【{self.account_id}】当前活跃消息处理任务数: {self.active_message_tasks}")

    def _unwrap_message_for_dedupe(self, message_data: dict) -> dict:
        if not isinstance(message_data, dict):
            return None

        if "1" in message_data:
            return message_data

        try:
            if not self.is_sync_package(message_data):
                return None

            sync_list = (((message_data.get("body") or {}).get("syncPushPackage") or {}).get("data") or [])
            if not sync_list:
                return None

            raw_data = sync_list[0].get("data")
            if not raw_data:
                return None

            decoded = base64.b64decode(raw_data).decode("utf-8")
            parsed = json.loads(decoded)
            return parsed if isinstance(parsed, dict) else None
        except Exception as e:
            logger.debug(f"【{self.account_id}】同步包去重解析失败: {self._safe_str(e)}")
            return None

    def _extract_message_id(self, message_data: dict) -> str:
        try:
            normalized_message = self._unwrap_message_for_dedupe(message_data)

            if isinstance(normalized_message, dict) and "1" in normalized_message:
                message_1 = normalized_message.get("1")
                if isinstance(message_1, dict) and "10" in message_1:
                    message_10 = message_1.get("10")
                    if isinstance(message_10, dict) and "bizTag" in message_10:
                        biz_tag = message_10.get("bizTag", "")
                        if isinstance(biz_tag, str):
                            try:
                                import json
                                biz_tag_dict = json.loads(biz_tag)
                                if isinstance(biz_tag_dict, dict) and "messageId" in biz_tag_dict:
                                    return biz_tag_dict.get("messageId")
                            except (json.JSONDecodeError, TypeError):
                                pass

                        if "extJson" in message_10:
                            ext_json = message_10.get("extJson", "")
                            if isinstance(ext_json, str):
                                try:
                                    import json
                                    ext_json_dict = json.loads(ext_json)
                                    if isinstance(ext_json_dict, dict) and "messageId" in ext_json_dict:
                                        return ext_json_dict.get("messageId")
                                except (json.JSONDecodeError, TypeError):
                                    pass
        except Exception as e:
            logger.debug(f"【{self.account_id}】提取消息ID失败: {self._safe_str(e)}")

        return None

    def _extract_message_id_from_chat_payload(self, message_1: dict, message_10: dict) -> str:
        try:
            if not isinstance(message_1, dict) or not isinstance(message_10, dict):
                return None

            biz_tag = message_10.get("bizTag", "")
            if isinstance(biz_tag, str) and biz_tag:
                try:
                    biz_tag_dict = json.loads(biz_tag)
                    if isinstance(biz_tag_dict, dict) and biz_tag_dict.get("messageId"):
                        return str(biz_tag_dict["messageId"])
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

            ext_json = message_10.get("extJson", "")
            if isinstance(ext_json, str) and ext_json:
                try:
                    ext_json_dict = json.loads(ext_json)
                    if isinstance(ext_json_dict, dict) and ext_json_dict.get("messageId"):
                        return str(ext_json_dict["messageId"])
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
        except Exception as e:
            logger.debug(f"【{self.account_id}】从聊天消息结构提取messageId失败: {self._safe_str(e)}")

        return None

    def _cleanup_message_reply_state(self, current_time: float):
        expired_processed_ids = [
            msg_id for msg_id, timestamp in self.processed_message_ids.items()
            if current_time - timestamp > self.message_expire_time
        ]
        for msg_id in expired_processed_ids:
            del self.processed_message_ids[msg_id]

        expired_pending_ids = [
            msg_id for msg_id, timestamp in self.pending_message_ids.items()
            if current_time - timestamp > self.pending_message_expire_time
        ]
        for msg_id in expired_pending_ids:
            del self.pending_message_ids[msg_id]

        if expired_processed_ids:
            logger.info(f"【{self.account_id}】已清理 {len(expired_processed_ids)} 个过期消息ID")
        if expired_pending_ids:
            logger.warning(f"【{self.account_id}】已清理 {len(expired_pending_ids)} 个超时未完成的消息预占")

        if len(self.processed_message_ids) > self.processed_message_ids_max_size:
            sorted_ids = sorted(self.processed_message_ids.items(), key=lambda x: x[1])
            remove_count = len(sorted_ids) // 2
            for msg_id, _ in sorted_ids[:remove_count]:
                del self.processed_message_ids[msg_id]
            logger.info(f"【{self.account_id}】消息ID去重字典过大，已清理 {remove_count} 个最旧记录")

    async def _reserve_message_reply(self, message_id: str) -> bool:
        async with self.processed_message_ids_lock:
            current_time = time.time()
            self._cleanup_message_reply_state(current_time)

            if message_id in self.processed_message_ids:
                last_process_time = self.processed_message_ids[message_id]
                time_elapsed = current_time - last_process_time
                remaining_time = int(max(0, self.message_expire_time - time_elapsed))
                logger.warning(f"【{self.account_id}】消息ID {message_id[:50]}... 已处理过，距离可重复回复还需 {remaining_time} 秒")
                return False

            if message_id in self.pending_message_ids:
                time_elapsed = current_time - self.pending_message_ids[message_id]
                remaining_time = int(max(0, self.pending_message_expire_time - time_elapsed))
                logger.warning(f"【{self.account_id}】消息ID {message_id[:50]}... 正在处理中，预占剩余约 {remaining_time} 秒")
                return False

            self.pending_message_ids[message_id] = current_time
            return True

    async def _finalize_message_reply(self, message_id: str, reason: str = ""):
        async with self.processed_message_ids_lock:
            current_time = time.time()
            self.pending_message_ids.pop(message_id, None)
            self.processed_message_ids[message_id] = current_time
            self._cleanup_message_reply_state(current_time)

        if reason:
            logger.info(f"【{self.account_id}】消息ID {message_id[:50]}... 已完成处理: {reason}")

    async def _release_message_reply(self, message_id: str, reason: str = ""):
        async with self.processed_message_ids_lock:
            released = self.pending_message_ids.pop(message_id, None)

        if released is not None:
            logger.warning(f"【{self.account_id}】消息ID {message_id[:50]}... 已释放预占，允许重试: {reason or 'unknown'}")

    def _is_websocket_usable(self, websocket) -> bool:
        if websocket is None:
            return False

        closed_flag = getattr(websocket, 'closed', None)
        if isinstance(closed_flag, bool):
            return not closed_flag

        close_code = getattr(websocket, 'close_code', None)
        if close_code is not None:
            return False

        return True

    async def _wait_for_reply_websocket(self, preferred_websocket=None, timeout: float = 8.0, poll_interval: float = 0.5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            current_ws = self.ws if self._is_websocket_usable(self.ws) else None
            if current_ws is not None:
                return current_ws

            if self._is_websocket_usable(preferred_websocket):
                return preferred_websocket

            await asyncio.sleep(poll_interval)

        return None

    async def _send_auto_reply_with_retry(self, websocket, chat_id: str, send_user_id: str, *,
                                          text: str = None, image_url: str = None,
                                          width: int = 800, height: int = 600):
        retry_delays = (0,) + tuple(self.auto_reply_send_retry_delays)
        last_error = None

        for attempt, delay in enumerate(retry_delays, start=1):
            if delay > 0:
                logger.warning(f"【{self.account_id}】自动回复发送将在 {delay} 秒后重试（第 {attempt} 次尝试）")
                await asyncio.sleep(delay)

            active_ws = await self._wait_for_reply_websocket(websocket, timeout=8.0)
            if not active_ws:
                last_error = RuntimeError("当前没有可用的WebSocket连接")
                logger.warning(f"【{self.account_id}】自动回复发送失败（第 {attempt} 次尝试）：{last_error}")
                continue

            try:
                if image_url is not None:
                    await self.send_image_msg(active_ws, chat_id, send_user_id, image_url, width=width, height=height)
                else:
                    await self.send_msg(active_ws, chat_id, send_user_id, text)
                return
            except Exception as e:
                last_error = e
                logger.warning(f"【{self.account_id}】自动回复发送失败（第 {attempt} 次尝试）：{self._safe_str(e)}")

        raise last_error or RuntimeError("自动回复发送失败")

    def _record_default_reply_once_after_success(self, chat_id: str):
        if not chat_id:
            return

        try:
            from db_manager import db_manager
            current_account_id = self._canonical_account_id()
            if not current_account_id:
                logger.error("【default】默认回复 reply_once 记录缺少 canonical account_id，拒绝继续运行")
                return
            default_reply_settings = db_manager.get_default_reply(current_account_id)
            if default_reply_settings and default_reply_settings.get('enabled', False) and default_reply_settings.get('reply_once', False):
                db_manager.add_default_reply_record(current_account_id, chat_id)
                logger.info(f"【{current_account_id}】记录默认回复成功发送: chat_id={chat_id}")
        except Exception as e:
            logger.error(f"【{self.account_id}】记录默认回复发送成功状态失败: {self._safe_str(e)}")

    async def _schedule_debounced_reply(self, chat_id: str, message_data: dict, websocket,
                                       send_user_name: str, send_user_id: str, send_message: str,
                                       item_id: str, msg_time: str, dedupe_message_id: str = None,
                                       dedupe_create_time: int = 0):
        message_id = str(dedupe_message_id).strip() if dedupe_message_id else self._extract_message_id(message_data)
        if not message_id:
            try:
                normalized_message = self._unwrap_message_for_dedupe(message_data) or {}
                create_time = int(dedupe_create_time or 0)
                if isinstance(normalized_message, dict) and "1" in normalized_message:
                    message_1 = normalized_message.get("1")
                    if isinstance(message_1, dict):
                        create_time = int(message_1.get("5", create_time) or create_time or 0)
                if not create_time:
                    create_time = int(time.time() * 1000)
                message_id = f"{chat_id}_{send_user_id}_{send_message}_{create_time}"
            except Exception:
                message_id = f"{chat_id}_{send_user_id}_{send_message}_{int(time.time() * 1000)}"

        if not await self._reserve_message_reply(message_id):
            return

        async with self.message_debounce_lock:
            if chat_id in self.message_debounce_tasks:
                old_message_id = (
                    self.message_debounce_tasks[chat_id]
                    .get('last_message', {})
                    .get('message_id')
                )
                old_task = self.message_debounce_tasks[chat_id].get('task')
                if old_task and not old_task.done():
                    old_task.cancel()
                    logger.warning(f"【{self.account_id}】取消chat_id {chat_id} 的旧防抖任务")
                if old_message_id and old_message_id != message_id:
                    await self._finalize_message_reply(old_message_id, reason="同会话出现更新消息，旧消息按防抖策略跳过")

            current_timer = time.time()
            self.message_debounce_tasks[chat_id] = {
                'last_message': {
                    'message_id': message_id,
                    'message_data': message_data,
                    'websocket': websocket,
                    'send_user_name': send_user_name,
                    'send_user_id': send_user_id,
                    'send_message': send_message,
                    'item_id': item_id,
                    'msg_time': msg_time
                },
                'timer': current_timer
            }

            async def debounce_task():
                saved_timer = current_timer
                try:
                    await asyncio.sleep(self.message_debounce_delay)

                    async with self.message_debounce_lock:
                        if chat_id not in self.message_debounce_tasks:
                            return

                        debounce_info = self.message_debounce_tasks[chat_id]
                        if saved_timer != debounce_info['timer']:
                            logger.warning(f"【{self.account_id}】chat_id {chat_id} 在防抖期间有新消息，跳过旧消息处理")
                            return

                        last_msg = debounce_info['last_message']

                        del self.message_debounce_tasks[chat_id]

                    logger.info(f"【{self.account_id}】防抖延迟结束，开始处理chat_id {chat_id} 的最后一条消息: {last_msg['send_message'][:30]}...")
                    reply_processed = await self._process_chat_message_reply(
                        last_msg['message_data'],
                        last_msg['websocket'],
                        last_msg['send_user_name'],
                        last_msg['send_user_id'],
                        last_msg['send_message'],
                        last_msg['item_id'],
                        chat_id,
                        last_msg['msg_time']
                    )
                    if reply_processed:
                        await self._finalize_message_reply(last_msg['message_id'], reason="回复链处理完成")
                    else:
                        await self._release_message_reply(last_msg['message_id'], reason="回复发失败，等待后续重试")

                except asyncio.CancelledError:
                    logger.warning(f"【{self.account_id}】chat_id {chat_id} 的防抖任务被取消")
                    try:
                        await self._release_message_reply(message_id, reason="防抖任务取消")
                    except Exception:
                        pass
                    current_task = asyncio.current_task()
                    async with self.message_debounce_lock:
                        if (
                            chat_id in self.message_debounce_tasks and
                            self.message_debounce_tasks[chat_id].get('task') is current_task
                        ):
                            del self.message_debounce_tasks[chat_id]
                except Exception as e:
                    logger.error(f"【{self.account_id}】处理防抖回复时发生错误: {self._safe_str(e)}")
                    try:
                        await self._release_message_reply(message_id, reason=f"防抖任务异常: {self._safe_str(e)}")
                    except Exception:
                        pass
                    current_task = asyncio.current_task()
                    async with self.message_debounce_lock:
                        if (
                            chat_id in self.message_debounce_tasks and
                            self.message_debounce_tasks[chat_id].get('task') is current_task
                        ):
                            del self.message_debounce_tasks[chat_id]

            task = self._create_tracked_task(debounce_task())
            self.message_debounce_tasks[chat_id]['task'] = task
            logger.warning(f"【{self.account_id}】为chat_id {chat_id} 创建防抖任务，延迟 {self.message_debounce_delay} 秒")

    async def _process_chat_message_reply(self, message_data: dict, websocket, send_user_name: str,
                                         send_user_id: str, send_message: str, item_id: str,
                                         chat_id: str, msg_time: str):
        try:
            if not AUTO_REPLY.get('enabled', True):
                logger.info(f"[{msg_time}] 【{self.account_id}】系统自动回复已禁用")
                return True

            canonical_account_id = self._canonical_account_id()

            if pause_manager.is_chat_paused(chat_id, account_id=canonical_account_id):
                remaining_time = pause_manager.get_remaining_pause_time(
                    chat_id,
                    account_id=canonical_account_id,
                )
                remaining_minutes = remaining_time // 60
                remaining_seconds = remaining_time % 60
                logger.info(f"[{msg_time}] 【{self.account_id}】系统chat_id {chat_id} 自动回复已暂停，剩余时间: {remaining_minutes}分{remaining_seconds}秒")
                return True

            reply = None
            reply_source = None

            reply = await self.get_item_specific_reply(send_user_name, send_user_id, send_message, item_id)
            if reply:
                reply_source = '指定商品'
            else:
                reply = await self.get_keyword_reply(send_user_name, send_user_id, send_message, item_id)
                if reply == "EMPTY_REPLY":
                    logger.info(f"[{msg_time}] 【{self.account_id}】匹配到空回复关键词，跳过自动回复")
                    return True
                elif reply:
                    reply_source = '关键词'  # 标记为关键词回复
                else:
                    reply = await self.get_default_reply(send_user_name, send_user_id, send_message, chat_id, item_id)
                    if reply == "EMPTY_REPLY":
                        logger.info(f"[{msg_time}] 【{self.account_id}】默认回复内容为空，跳过自动回复")
                        return True
                    elif reply == "SKIP_REPLY":
                        logger.info(f"[{msg_time}] 【{self.account_id}】默认回复已命中过当前会话，跳过自动回复")
                        return True
                    elif reply:
                        reply_source = '默认'
                    else:
                        reply = await self.get_ai_reply(send_user_name, send_user_id, send_message, item_id, chat_id)
                        if reply:
                            reply_source = 'AI'

            if reply:
                if reply.startswith("__IMAGE_SEND__"):
                    image_url = reply.replace("__IMAGE_SEND__", "")
                    try:
                        await self._send_auto_reply_with_retry(
                            websocket,
                            chat_id,
                            send_user_id,
                            image_url=image_url,
                        )
                        msg_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                        logger.info(f"[{msg_time}] 【{reply_source}图片发出】用户: {send_user_name} (ID: {send_user_id}), 商品({item_id}): 图片 {image_url}")
                        if reply_source == '默认':
                            self._record_default_reply_once_after_success(chat_id)
                    except Exception as e:
                        logger.error(f"图片发送失败: {self._safe_str(e)}")
                        await self._send_auto_reply_with_retry(
                            websocket,
                            chat_id,
                            send_user_id,
                            text="抱歉，图片发送失败，请稍后重试试。"
                        )
                        msg_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                        logger.error(f"[{msg_time}] 【{reply_source}图片发送失败】用户: {send_user_name} (ID: {send_user_id}), 商品({item_id})")
                        if reply_source == '默认':
                            self._record_default_reply_once_after_success(chat_id)
                else:
                    await self._send_auto_reply_with_retry(
                        websocket,
                        chat_id,
                        send_user_id,
                        text=reply
                    )
                    msg_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                    logger.info(f"[{msg_time}] 【{reply_source}发出】用户: {send_user_name} (ID: {send_user_id}), 商品({item_id}): {reply}")
                    if reply_source == '默认':
                        self._record_default_reply_once_after_success(chat_id)
            else:
                msg_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                logger.info(f"[{msg_time}] 【{self.account_id}】【系统】未找到匹配的回复规则，不回复")
            return True
        except Exception as e:
            logger.error(f"处理聊天消息回复时发生错误: {self._safe_str(e)}")
            return False

    async def handle_message(self, message_data, websocket, msg_id="unknown"):
        msg_size = len(json.dumps(message_data)) if message_data else 0
        canonical_account_id = self._canonical_account_id()
        logger.info(f"【{self.account_id}】[{msg_id}] 🚀 开始处理消息 ({msg_size}字节)")

        def _has_canonical_order_scope() -> bool:
            return bool(canonical_account_id)

        def _log_missing_canonical_order_scope(operation_label: str):
            logger.error(
                f"【default】消息订单链缺少 canonical account_id，拒绝继续运行 {operation_label}"
            )

        try:
            if not self._is_current_account_enabled():
                logger.warning(f"【{self.account_id}】[{msg_id}] ⏹️ 账号已禁用，消息处理结束")
                return

            try:
                message = message_data
                ack = {
                    "code": 200,
                    "headers": {
                        "mid": message["headers"]["mid"] if "mid" in message["headers"] else generate_mid(),
                        "sid": message["headers"]["sid"] if "sid" in message["headers"] else '',
                    }
                }
                if 'app-key' in message["headers"]:
                    ack["headers"]["app-key"] = message["headers"]["app-key"]
                if 'ua' in message["headers"]:
                    ack["headers"]["ua"] = message["headers"]["ua"]
                if 'dt' in message["headers"]:
                    ack["headers"]["dt"] = message["headers"]["dt"]
                await websocket.send(json.dumps(ack))
            except Exception as e:
                logger.debug(f"【{self.account_id}】[{msg_id}] 发ACK失败: {e}")

            if not self.is_sync_package(message_data):
                logger.debug(f"【{self.account_id}】[{msg_id}] ⏹️ 非同步包消息，处理结束")
                return

            sync_data = message_data["body"]["syncPushPackage"]["data"][0]

            if "data" not in sync_data:
                logger.warning(f"【{self.account_id}】[{msg_id}] ⚠️ 同步包中无data字段，消息内容: {sync_data}")
                logger.warning(f"【{self.account_id}】[{msg_id}] ⏹️ 消息处理结束（缺少data字段）")
                return

            message = None
            try:
                data = sync_data["data"]
                logger.debug(f"【{self.account_id}】[{msg_id}] 开始解密同步包数据...")
                try:
                    data = base64.b64decode(data).decode("utf-8")
                    parsed_data = json.loads(data)
                    msg_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                    if isinstance(parsed_data, dict) and 'chatType' in parsed_data:
                        logger.warning(f"【{self.account_id}】[{msg_id}] ⚠️ 检测到chatType消息，完整内容: {parsed_data}")
                        if 'operation' in parsed_data and 'content' in parsed_data['operation']:
                            content = parsed_data['operation']['content']
                            if 'sessionArouse' in content:
                                logger.info(f"[{msg_time}] 【{self.account_id}】[{msg_id}] 【系统小闲鱼智能提示:")
                                if 'arouseChatScriptInfo' in content['sessionArouse']:
                                    for qa in content['sessionArouse']['arouseChatScriptInfo']:
                                        logger.info(f"  - {qa['chatScrip']}")
                                logger.info(f"[{msg_time}] 【{self.account_id}】[{msg_id}] ⏹️ 系统引导消息处理完成")
                                return
                            elif 'contentType' in content:
                                logger.warning(f"[{msg_time}] 【{self.account_id}】[{msg_id}] 【系统】其他类型消息: {content}")
                        logger.warning(f"【{self.account_id}】[{msg_id}] ⚠️ chatType消息但不是引导消息，继续处理...")
                        message = parsed_data
                    else:
                        logger.debug(f"【{self.account_id}】[{msg_id}] 解密成功，正常消息")
                        message = parsed_data
                except Exception as e:
                    logger.debug(f"【{self.account_id}】[{msg_id}] JSON解析失败，尝试解密...")
                    decrypted_data = decrypt(data)
                    message = json.loads(decrypted_data)
                    logger.debug(f"【{self.account_id}】[{msg_id}] 解密成功")
            except Exception as e:
                logger.error(f"【{self.account_id}】[{msg_id}] ❌ 消息解密失败: {self._safe_str(e)}")
                if msg_size > 3000:
                    logger.error(f"【{self.account_id}】[{msg_id}] ⚠️⚠️⚠️ 大消息({msg_size}字节)解密失败，完整sync_data: {sync_data}")
                    try:
                        raw_data = sync_data.get("data", "")
                        logger.error(f"【{self.account_id}】[{msg_id}] Base64数据长度: {len(raw_data)}")
                        logger.error(f"【{self.account_id}】[{msg_id}] Base64前100字符: {raw_data[:100]}")
                        logger.error(f"【{self.account_id}】[{msg_id}] Base64前100字符: {raw_data[-100:]}")
                    except Exception:
                        pass
                logger.error(f"【{self.account_id}】[{msg_id}] ⏹️ 消息处理结束（解密失败）")
                return

            if message is None:
                logger.error(f"【{self.account_id}】[{msg_id}] ❌ 消息解析后为空")
                if msg_size > 3000:
                    logger.error(f"【{self.account_id}】[{msg_id}] ⚠️⚠️⚠️ 大消息({msg_size}字节)解析后为空！")
                logger.error(f"【{self.account_id}】[{msg_id}] ⏹️ 消息处理结束（解析后为空）")
                return

            if not isinstance(message, dict):
                logger.error(f"【{self.account_id}】[{msg_id}] ❌ 消息格式错误，期望字典但得到: {type(message)}")
                logger.warning(f"【{self.account_id}】[{msg_id}] 消息内容: {message}")
                logger.error(f"【{self.account_id}】[{msg_id}] ⏹️ 消息处理结束（格式错误）")
                return

            self.last_message_received_time = time.time()
            logger.warning(f"【{self.account_id}】[{msg_id}] ✅ 开始处理消息")

            order_id = None
            try:
                logger.info(f"【{self.account_id}】[{msg_id}] 🔍 开始提取订单ID，消息类型: {type(message)}")
                order_id = self._extract_order_id(message, message_data)
                if order_id:
                    msg_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                    logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] ✅ 检测到订单ID: {order_id}，开始获取订单详情')

                    order_context = self._extract_order_message_context(message, msg_id=msg_id)
                    temp_user_id = order_context.get('buyer_id')
                    temp_user_id_source = order_context.get('buyer_id_source')
                    temp_item_id = order_context.get('item_id')
                    temp_sid = order_context.get('sid')
                    temp_buyer_nick = order_context.get('buyer_nick')

                    if self.order_status_handler:
                        logger.info(f"【{self.account_id}】准备调用订单状态处理器.on_order_id_extracted: {order_id}")
                        if not _has_canonical_order_scope():
                            _log_missing_canonical_order_scope(
                                f"跳过订单ID提取通知: order_id={order_id}"
                            )
                        else:
                            try:
                                self.order_status_handler.on_order_id_extracted(
                                    order_id=order_id,
                                    account_id=canonical_account_id,
                                    message=message,
                                    match_context={
                                        'sid': temp_sid,
                                        'buyer_id': temp_user_id,
                                        'item_id': temp_item_id,
                                    }
                                )
                                logger.info(f"【{self.account_id}】订单状态处理器.on_order_id_extracted调用成功: {order_id}")
                            except Exception as e:
                                logger.error(f"【{self.account_id}】通知订单状态处理器订单ID提取失败: {self._safe_str(e)}")
                                import traceback
                                logger.error(f"【{self.account_id}】详细错误信息: {traceback.format_exc()}")
                    else:
                        logger.warning(f"【{self.account_id}】订单状态处理器为None，跳过订单ID提取通知: {order_id}")

                    basic_order_saved = False
                    if not _has_canonical_order_scope():
                        _log_missing_canonical_order_scope(
                            f"跳过订单预入库与详情抓取: order_id={order_id}"
                        )
                        order_id = None
                    else:
                        basic_order_saved = self._preload_basic_order_info(
                            order_id,
                            item_id=temp_item_id,
                            buyer_id=temp_user_id,
                            sid=temp_sid,
                            buyer_nick=temp_buyer_nick,
                            buyer_id_source=temp_user_id_source,
                        )

                        try:
                            order_detail = await self.fetch_order_detail_info(
                                order_id,
                                temp_item_id,
                                temp_user_id,
                                sid=temp_sid,
                                buyer_nick=temp_buyer_nick,
                                buyer_id_source=temp_user_id_source,
                            )
                            if order_detail:
                                logger.info(f'[{msg_time}] 【{self.account_id}】✅ 订单详情获取成功: {order_id}')
                            else:
                                logger.warning(f'[{msg_time}] 【{self.account_id}】⚠️ 订单详情获取失败: {order_id}')
                                if basic_order_saved:
                                    self._schedule_order_detail_retry(
                                        order_id,
                                        item_id=temp_item_id,
                                        buyer_id=temp_user_id,
                                        sid=temp_sid,
                                        buyer_nick=temp_buyer_nick,
                                        delay_seconds=30,
                                        buyer_id_source=temp_user_id_source,
                                    )

                        except Exception as detail_e:
                            logger.error(f'[{msg_time}] 【{self.account_id}】❌ 获取订单详情异常: {self._safe_str(detail_e)}')
                            if basic_order_saved:
                                self._schedule_order_detail_retry(
                                    order_id,
                                    item_id=temp_item_id,
                                    buyer_id=temp_user_id,
                                    sid=temp_sid,
                                    buyer_nick=temp_buyer_nick,
                                    delay_seconds=30,
                                    buyer_id_source=temp_user_id_source,
                                )
                else:
                    logger.warning(f"【{self.account_id}】[{msg_id}] 未检测到订单ID")
            except Exception as e:
                logger.error(f"【{self.account_id}】[{msg_id}] 提取订单ID失败: {self._safe_str(e)}")

            user_id = None
            try:
                message_1 = message.get("1")
                if isinstance(message_1, str):
                    message_4 = message.get("4")
                    if isinstance(message_4, dict):
                        user_id = message_4.get("senderUserId") or None
                elif isinstance(message_1, dict):
                    if "10" in message_1 and isinstance(message_1["10"], dict):
                        user_id = message_1["10"].get("senderUserId") or None
                    else:
                        user_id = None
                else:
                    user_id = None
            except Exception as e:
                logger.warning(f"提取用户ID失败: {self._safe_str(e)}")
                user_id = None



            item_id = None
            try:
                if "1" in message and isinstance(message["1"], dict) and "10" in message["1"] and isinstance(message["1"]["10"], dict):
                    url_info = message["1"]["10"].get("reminderUrl", "")
                    if isinstance(url_info, str) and "itemId=" in url_info:
                        item_id = url_info.split("itemId=")[1].split("&")[0]

                if not item_id:
                    item_id = self.extract_item_id_from_message(message)

                if not item_id:
                    item_id = f"auto_{user_id}_{int(time.time())}"
                    logger.warning(f"无法提取商品ID，使用默认值: {item_id}")

            except Exception as e:
                logger.error(f"提取商品ID时发生错误: {self._safe_str(e)}")
                item_id = f"auto_{user_id}_{int(time.time())}"
            try:
                logger.info(f"【{self.account_id}】[{msg_id}] 消息内容: {message}")
                msg_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

                red_reminder = None
                if isinstance(message, dict) and "3" in message and isinstance(message["3"], dict):
                    red_reminder = message["3"].get("redReminder")

                if red_reminder == '等待买家付款':
                    user_url = f'https://www.goofish.com/personal?userId={user_id}'
                    logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 【系统】等待买家 {user_url} 付款')
                    logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（等待买家付款）")
                    return
                elif red_reminder == '交易关闭':
                    user_url = f'https://www.goofish.com/personal?userId={user_id}'
                    logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 【系统】买家 {user_url} 交易关闭')

                    if self.order_status_handler:
                        if not _has_canonical_order_scope():
                            _log_missing_canonical_order_scope(
                                f"跳过交易关闭状态回填 buyer_id={user_id or 'unknown'}"
                            )
                        else:
                            try:
                                self.order_status_handler.handle_red_reminder_order_status(
                                    red_reminder=red_reminder,
                                    message=message,
                                    user_id=user_id,
                                    account_id=canonical_account_id,
                                    msg_time=msg_time,
                                    match_context={
                                        'sid': None,
                                        'buyer_id': user_id,
                                        'item_id': item_id,
                                    }
                                )
                            except Exception as e:
                                logger.error(f"【{self.account_id}】更新交易关闭订单状态失败: {self._safe_str(e)}")

                    logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（交易关闭）")
                    return
                elif red_reminder == '等待卖家发货':
                    user_url = f'https://www.goofish.com/personal?userId={user_id}'
                    logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 【系统】交易成功 {user_url} 等待卖家发货')

                    if isinstance(message.get('1'), str):
                        if not _has_canonical_order_scope():
                            _log_missing_canonical_order_scope(
                                f"跳过简化待发货自动发货: sid={message.get('1') or 'unknown'}"
                            )
                            return
                        logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 🔔 检测到简化结构的发货通知消息，延迟处理')
                        await asyncio.sleep(30)
                        logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 🔔 延迟30秒后处理简化发货')
                        if self.is_auto_confirm_enabled():
                            logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] ✅ 自动确认发货已启用，开始处理')

                            simple_sid = message.get('1', '')
                            session_id_str = simple_sid.split('@')[0] if '@' in str(simple_sid) else simple_sid

                            logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 🔍 简化消息解析: sid={simple_sid}, session_id={session_id_str}')

                            log_prefix = f'[{msg_time}] 【{self.account_id}】[{msg_id}]'
                            sid_lookup_minutes = 5
                            sid_lookup = self._lookup_delivery_order_by_sid(
                                simple_sid,
                                item_id=item_id,
                                buyer_id=user_id,
                                minutes=sid_lookup_minutes,
                                log_prefix=log_prefix
                            )
                            sid_lookup = await self._refresh_sid_lookup_if_needed(
                                simple_sid,
                                sid_lookup,
                                item_id=item_id,
                                buyer_id=user_id,
                                minutes=sid_lookup_minutes,
                                allow_bargain_ready=True,
                                log_prefix=log_prefix
                            )
                            recent_order = sid_lookup.get('order')
                            sid_match_type = sid_lookup.get('match_type', 'missing')

                            if recent_order and sid_match_type in {'pending_ship', 'bargain_ready'}:
                                order_id = recent_order.get('order_id')
                                real_item_id = recent_order.get('item_id')
                                simple_user_id = recent_order.get('buyer_id', user_id)
                                logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] ✅ 通过sid从数据库找到订单: order_id={order_id}, item_id={real_item_id}, buyer_id={simple_user_id}')

                                if sid_match_type == 'bargain_ready':
                                    logger.info(
                                        f'[{msg_time}] 【{self.account_id}】[{msg_id}] ✅ 小刀订单缺少完整待发货卡片，'
                                        f'使用sid+小刀成功证据兜底进入自动发货: order_id={order_id}'
                                    )

                                if not self.can_auto_delivery(order_id):
                                    logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 🔒 订单 {order_id} 已在冷却期内（可能完整消息已处理），跳过简化消息发货')
                                    logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（订单已处理）")
                                    return

                                delivery_lock_key = self._compose_order_delivery_scope_key(
                                    canonical_account_id,
                                    order_id,
                                )
                                if not delivery_lock_key:
                                    _log_missing_canonical_order_scope(
                                        f"跳过简化消息延迟锁作用域: order_id={order_id}"
                                    )
                                    return

                                if self.is_lock_held(delivery_lock_key):
                                    logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 🔒 订单 {order_id} 延迟锁已被持有（可能完整消息正在处理），跳过简化消息发货')
                                    logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（订单正在处理）")
                                    return

                                simple_chat_id = session_id_str

                                await self._handle_simple_message_auto_delivery(
                                    websocket=websocket,
                                    order_id=order_id,
                                    item_id=real_item_id,
                                    user_id=simple_user_id,
                                    chat_id=simple_chat_id,
                                    msg_time=msg_time,
                                    msg_id=msg_id
                                )
                                logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（简化消息自动发货）")
                                return
                            elif recent_order:
                                order_id = recent_order.get('order_id')
                                order_status = recent_order.get('order_status') or 'unknown'
                                if sid_match_type == 'already_processed':
                                    logger.info(
                                        f'[{msg_time}] 【{self.account_id}】[{msg_id}] ℹ️ sid命中的订单已处理完成，跳过重复发货: '
                                        f'order_id={order_id}, status={order_status}'
                                    )
                                    logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（订单已处理）")
                                elif sid_match_type == 'cancelled':
                                    logger.info(
                                        f'[{msg_time}] 【{self.account_id}】[{msg_id}] ℹ️ sid命中的订单已关闭，跳过自动发货: '
                                        f'order_id={order_id}'
                                    )
                                    logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（订单已关闭）")
                                else:
                                    logger.info(
                                        f'[{msg_time}] 【{self.account_id}】[{msg_id}] ℹ️ sid命中的订单当前状态不适合简化消息兜底发货，等待后续完整消息: '
                                        f'order_id={order_id}, status={order_status}'
                                    )
                                    logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（订单状态未就绪）")
                                return
                            elif sid_match_type.startswith('ambiguous_'):
                                logger.warning(
                                    f'[{msg_time}] 【{self.account_id}】[{msg_id}] sid命中多个候选订单，严格模式拒绝简化消息自动发货: '
                                    f'sid={simple_sid}, match_type={sid_match_type}'
                                )
                                logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（sid候歧义）")
                                return
                            else:
                                logger.warning(f'[{msg_time}] 【{self.account_id}】[{msg_id}] ❌ 未找到sid {simple_sid} 的最近订单，跳过自动发货')
                                logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（未找到订单）")
                                return
                        else:
                            logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] ⚠️ 未启用自动确认发货，跳过')
                            logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（未启用自动发货）")
                            return
            except Exception:
                pass

            if not self.is_chat_message(message):
                logger.warning(f"【{self.account_id}】[{msg_id}] ⏹️ 非聊天消息，处理结束")
                return

            try:
                if not (isinstance(message, dict) and "1" in message and isinstance(message["1"], dict)):
                    logger.error(f"【{self.account_id}】[{msg_id}] ❌ 消息格式错误：缺少必要的字段结构")
                    logger.error(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（格式错误）")
                    return

                message_1 = message["1"]
                if not isinstance(message_1.get("10"), dict):
                    logger.error(f"【{self.account_id}】[{msg_id}] ❌ 消息格式错误：缺少消息详情字段")
                    logger.error(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（缺少详情字段）")
                    return

                create_time = int(message_1.get("5", 0))
                message_10 = message_1["10"]
                send_user_name = message_10.get("senderNick", message_10.get("reminderTitle", "未知用户"))
                send_user_id = message_10.get("senderUserId", "unknown")
                send_message = message_10.get("reminderContent", "")
                dedupe_message_id = self._extract_message_id_from_chat_payload(message_1, message_10)

                chat_id_raw = message_1.get("2", "")
                chat_id = chat_id_raw.split('@')[0] if '@' in str(chat_id_raw) else str(chat_id_raw)

            except Exception as e:
                logger.error(f"【{self.account_id}】[{msg_id}] ❌ 提取聊天消息信息失败: {self._safe_str(e)}")
                logger.error(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（提取信息失败）")
                return

            msg_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(create_time/1000))



            message_route_info = self._classify_message_route(
                message=message,
                message_1=message_1,
                message_10=message_10,
                send_message=send_message,
            )
            message_route = message_route_info.get('route', 'user_chat')
            order_status_signal = message_route_info.get('order_status_signal')
            should_notify_message = bool(message_route_info.get('should_notify'))
            allow_auto_reply = bool(message_route_info.get('allow_auto_reply'))
            is_system_message = bool(message_route_info.get('is_system_message'))
            is_group_message = bool(message_route_info.get('is_group_message'))
            message_direction = message_route_info.get('message_direction', 0)
            content_type = message_route_info.get('content_type', 0)
            card_title = str(message_route_info.get('card_title') or '').strip()
            special_flow_card_titles = {
                '我已小刀，待刀成',
                self._legacy_replace_tail('我已小刀，待刀成', '刀成', '\u5222'),
                self._legacy_drop_last_char('我已小刀,待刀成'),
                '我已成功小刀，待发货',
                self._legacy_drop_last_char('我已成功小刀,待发货'),
            }

            logger.info(
                f"【{self.account_id}】[{msg_id}] 消息分类: route={message_route}, "
                f"status_signal={order_status_signal or 'none'}, notify={should_notify_message}, "
                f"auto_reply={allow_auto_reply}, system={is_system_message}, "
                f"direction={message_direction}, contentType={content_type}"
            )

            if send_user_id == self.myid and not is_system_message:
                logger.info(f"[{msg_time}] 【{self.account_id}】[{msg_id}] 【手动发出】 商品({item_id}): {send_message}")

                if not canonical_account_id:
                    logger.error(
                        f"【default】手动发消息暂停链缺少 canonical account_id，拒绝继续暂停 chat: {chat_id}"
                    )
                    return
                pause_manager.pause_chat(chat_id, canonical_account_id)

                logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（手动发出消息）")
                return
            elif send_user_id == self.myid and is_system_message:
                logger.info(
                    f"[{msg_time}] 【{self.account_id}】[{msg_id}] 检测到系统消息(sender=自己ID)，继续执行状态处理 "
                    f"(direction={message_direction}, contentType={content_type})"
                )
            else:
                logger.info(f"[{msg_time}] 【收到】用户: {send_user_name} (ID: {send_user_id}), 商品({item_id}): {send_message}")
                if message_route == 'user_chat':
                    self.last_user_chat_time = time.time()

                async with self.yifan_account_lock:
                    if chat_id in self.yifan_account_waiting:
                        waiting_info = self.yifan_account_waiting[chat_id]

                        if time.time() - waiting_info['create_time'] > 1800:
                            logger.warning(f"账号输入等待超时，清除等待状态")
                            del self.yifan_account_waiting[chat_id]
                        elif waiting_info['buyer_id'] == send_user_id:
                            message_1 = message.get('1', {})
                            message_direction = message_1.get('7', 0) if isinstance(message_1, dict) else 0

                            content_type = 0
                            try:
                                message_6 = message_1.get('6', {})
                                if isinstance(message_6, dict):
                                    message_6_3 = message_6.get('3', {})
                                    if isinstance(message_6_3, dict):
                                        content_type = message_6_3.get('4', 0)
                            except Exception:
                                pass

                            is_system_msg = False
                            try:
                                message_10 = message_1.get('10', {})
                                if isinstance(message_10, dict):
                                    biz_tag = message_10.get('bizTag', '')
                                    if biz_tag and ('SECURITY' in biz_tag or 'taskName' in biz_tag or 'taskId' in biz_tag):
                                        is_system_msg = True
                            except Exception:
                                pass

                            if message_direction != 2 or content_type == 6 or is_system_msg:
                                logger.info(f"【{self.account_id}】[{msg_id}] 收到系统消息，跳过账号确认处理（direction={message_direction}, contentType={content_type}, isSystem={is_system_msg}）")
                                logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（系统消息）")
                                return

                            if waiting_info['state'] == 'waiting_account':
                                account = send_message.strip()
                                if account:
                                    waiting_info['state'] = 'waiting_confirm'

                                    confirm_msg = f"{account}\n这是您要充值的账号，请回答\"是\"，进行确认下单，如果账号不对，请重新输入正确的账号，如果因为您账号输错，导致错误下单，概不退款。"
                                    await self.send_msg(self.ws, chat_id, send_user_id, confirm_msg)
                                    logger.info(f"【{self.account_id}】[{msg_id}] 已保存充值账号: {account}，等待用户确认")
                                    logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（等待账号确认）")
                                    return
                            elif waiting_info['state'] == 'waiting_confirm':
                                user_reply = send_message.strip()

                                if user_reply == '是':
                                    logger.info(f"用户确认账号，继续亦凡API发货流程")
                                    account = waiting_info['account']
                                    rule = waiting_info['rule']
                                    order_id_saved = waiting_info.get('order_id')
                                    item_id_saved = waiting_info.get('item_id')


                                    try:
                                        delivery_content = await self._call_yifan_api_with_account(
                                            rule, account, order_id_saved, item_id_saved, send_user_id, chat_id
                                        )

                                        if delivery_content:
                                            delivery_steps = self._build_delivery_steps(
                                                delivery_content,
                                                rule.get('card_description', '')
                                            )
                                            await self._send_delivery_steps(
                                                self.ws,
                                                chat_id,
                                                send_user_id,
                                                delivery_steps,
                                                log_prefix=f"亦凡账号确认发货 order_id={order_id_saved or 'unknown'}"
                                            )

                                            finalize_result = await self._finalize_delivery_after_send(
                                                delivery_meta={
                                                    'success': True,
                                                    'rule_id': rule.get('id'),
                                                    'card_id': rule.get('card_id'),
                                                    'card_type': rule.get('card_type'),
                                                    'order_spec_mode': None,
                                                    'rule_spec_mode': None,
                                                    'item_config_mode': None,
                                                    'data_card_pending_consume': False,
                                                    'data_line': None
                                                },
                                                order_id=order_id_saved,
                                                item_id=item_id_saved
                                            )
                                            if not finalize_result.get('success'):
                                                self._record_delivery_log(
                                                    order_id=order_id_saved,
                                                    item_id=item_id_saved,
                                                    buyer_id=send_user_id,
                                                    status='failed',
                                                    reason=finalize_result.get('error') or '亦凡账号确认发货发成功但提交副作用失',
                                                    channel='auto',
                                                    rule_meta={
                                                        'rule_id': rule.get('id'),
                                                        'rule_keyword': rule.get('keyword'),
                                                        'card_type': rule.get('card_type')
                                                    }
                                                )
                                                await self.send_msg(self.ws, chat_id, send_user_id, "发货消息已发送，但确认发货失败，请稍后刷新订单状态。")
                                                logger.error(f"亦凡API自动发货副作用提交失败: {finalize_result.get('error')}")
                                                return

                                            if order_id_saved:
                                                self.mark_delivery_sent(order_id_saved, context="亦凡账号确认发货发送成功")
                                                delivery_lock_key = self._compose_order_delivery_scope_key(
                                                    canonical_account_id,
                                                    order_id_saved,
                                                )
                                                if delivery_lock_key:
                                                    self._activate_delivery_lock(delivery_lock_key, delay_minutes=10)
                                                else:
                                                    _log_missing_canonical_order_scope(
                                                        f"跳过亦凡账号确认发货延迟锁激活 order_id={order_id_saved}"
                                                    )

                                            self._record_delivery_log(
                                                order_id=order_id_saved,
                                                item_id=item_id_saved,
                                                buyer_id=send_user_id,
                                                status='success',
                                                reason='亦凡账号确认发货发送成功',
                                                channel='auto',
                                                rule_meta={
                                                    'rule_id': rule.get('id'),
                                                    'rule_keyword': rule.get('keyword'),
                                                    'card_type': rule.get('card_type')
                                                }
                                            )
                                            logger.info(f"亦凡API自动发货成功")
                                        else:
                                            await self.send_msg(self.ws, chat_id, send_user_id, "抱歉，自动发货失败，请联系客服处理。")
                                    except Exception as e:
                                        logger.error(f"亦凡API发货异常: {self._safe_str(e)}")
                                        await self.send_msg(self.ws, chat_id, send_user_id, "系统异常，请联系客服处理。")

                                    return

                                else:
                                    new_account = user_reply
                                    if new_account:
                                        waiting_info['account'] = new_account
                                        waiting_info['retry_count'] += 1

                                        if waiting_info['retry_count'] >= 5:
                                            logger.warning(f"【{self.account_id}】[{msg_id}] 账号确认重试次数过多，取消发货")
                                            del self.yifan_account_waiting[chat_id]
                                            await self.send_msg(self.ws, chat_id, send_user_id, "账号确认失败次数过多，已取消发货，请重新下单。")
                                            logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（重试次数过多）")
                                            return

                                        confirm_msg = f"{new_account}\n这是您要充值的账号，请回答\"是\"，进行确认下单，如果账号不对，请重新输入正确的账号，如果因为您账号输错，导致错误下单，概不退款。"
                                        await self.send_msg(self.ws, chat_id, send_user_id, confirm_msg)
                                        logger.info(f"【{self.account_id}】[{msg_id}] 用户重新输入账号: {new_account}，再次等待确认")
                                        logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（等待账号重新确认）")
                                        return

                try:
                    if is_group_message:
                        logger.info(f"📱 棢测到群组消息（sessionType=30），跳过消息通知")
                    elif should_notify_message:
                        await self.send_notification(send_user_name, send_user_id, send_message, item_id, chat_id)
                    else:
                        logger.info(
                            f"📱 当前消息不发送通知: route={message_route}, "
                            f"status_signal={order_status_signal or 'none'}, message={send_message}"
                        )
                except Exception as notify_error:
                    logger.error(f"📱 发消息知失败: {self._safe_str(notify_error)}")




            if self.order_status_handler:
                try:
                    handled = False
                    if not _has_canonical_order_scope():
                        _log_missing_canonical_order_scope(
                            f"跳过系统订单状态处理 msg_id={msg_id}"
                        )
                    else:
                        try:
                            handled = self.order_status_handler.handle_system_message(
                                message=message,
                                send_message=send_message,
                                account_id=canonical_account_id,
                                msg_time=msg_time,
                                match_context={
                                    'sid': message_1.get('2', '') if isinstance(message_1, dict) else None,
                                    'buyer_id': send_user_id,
                                    'item_id': item_id,
                                }
                            )
                        except Exception as e:
                            logger.error(f"【{self.account_id}】处理系统消息失败: {self._safe_str(e)}")
                            handled = False

                    if not handled and _has_canonical_order_scope():
                        try:
                            if isinstance(message, dict) and "3" in message and isinstance(message["3"], dict):
                                red_reminder = message["3"].get("redReminder")
                                user_id = message["3"].get("userId", "unknown")

                                if red_reminder:
                                    try:
                                        self.order_status_handler.handle_red_reminder_message(
                                            message=message,
                                            red_reminder=red_reminder,
                                            user_id=user_id,
                                            account_id=canonical_account_id,
                                            msg_time=msg_time,
                                            match_context={
                                                'sid': message_1.get('2', '') if isinstance(message_1, dict) else None,
                                                'buyer_id': send_user_id,
                                                'item_id': item_id,
                                            }
                                        )
                                    except Exception as e:
                                        logger.error(f"【{self.account_id}】处理红色提醒消息失败: {self._safe_str(e)}")
                        except Exception as red_e:
                            logger.warning(f"处理红色提醒消息失败: {self._safe_str(red_e)}")

                except Exception as e:
                    logger.error(f"订单状态处理失败: {self._safe_str(e)}")

            if order_id and order_status_signal in {'pending_ship', 'shipped', 'completed', 'cancelled', 'refunding'}:
                try:
                    refresh_sid = ''
                    if isinstance(message_1, dict):
                        refresh_sid = message_1.get("2", "")

                    await self._maybe_force_refresh_order_detail_for_signal(
                        order_id=order_id,
                        item_id=item_id,
                        buyer_id=send_user_id,
                        sid=refresh_sid,
                        buyer_nick=send_user_name,
                        status_signal=order_status_signal,
                        reason=f'message_signal_{order_status_signal}',
                        delay_seconds=1 if order_status_signal == 'pending_ship' else 0,
                        log_prefix=f"【{self.account_id}】[{msg_id}]"
                    )
                except Exception as refresh_e:
                    logger.error(
                        f"【{self.account_id}】[{msg_id}] 状态消息触发订单详情补刷失败: {self._safe_str(refresh_e)}"
                    )

            fallback_ignore_keywords = [
                '不想宝贝被砍',
                'AI正在帮你回复',
                '发来丢',
                '小心假客服骗',
                '蚂蚁森林能量',
                '恭喜你拿到曝光卡',
                '订单即将自动确认收货',
                '温馨提醒：商品信息近期有过变',
            ]
            if send_message == '[我已拍下，待付款]':
                logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 系统消息不处理')
                logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（系统消息：待付款）")
                return
            elif send_message == '[你关闭了订单，钱款已原路退返]':
                logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 系统消息不处理')
                logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（系统消息：订单关闭）")
                return
            elif send_message in [
                '快给ta丢个评价吧~',
                '快给ta丢个评价吧',
            ]:
                logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 🌟 棢测到评价提醒消息: {send_message}')
                await self.handle_auto_comment(message, msg_time, msg_id)
                logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（评价提醒消息）")
                return
            elif message_route == 'system_notice' or any(keyword in send_message for keyword in fallback_ignore_keywords):
                logger.info(
                    f'[{msg_time}] 【{self.account_id}】[{msg_id}] ⏹️ 系统提示消息不处理: '
                    f'route={message_route}, message={send_message}'
                )
                return
            elif message_route == 'order_status' and self._is_auto_delivery_trigger(send_message):
                logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 棢测到自动发货触发消息: {send_message}')

                if not is_system_message:
                    logger.warning(
                        f'[{msg_time}] 【{self.account_id}】[{msg_id}] ⚠️ 自动发货关键字来自非系统消息，已忽略 '
                        f'(direction={message_direction}, contentType={content_type})'
                    )
                    logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（非系统触发）")
                    return

                if not _has_canonical_order_scope():
                    _log_missing_canonical_order_scope(
                        f"跳过订单状态自动发货 message={send_message or 'unknown'}"
                    )
                    return

                if not self.is_auto_confirm_enabled():
                    logger.info(f'[{msg_time}] 【{self.account_id}】[{msg_id}] 未启用自动确认发货，跳过自动发货')
                    logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（未启用自动发货）")
                    return
                await self._handle_auto_delivery(websocket, message, send_user_name, send_user_id,
                                               item_id, chat_id, msg_time, message_data)
                logger.info(f"【{self.account_id}】[{msg_id}] ⏹️ 处理结束（自动发货完成）")
                return
            elif send_message == '[卡片消息]' or card_title in special_flow_card_titles:
                try:
                    card_title = card_title or None
                    card_message_1 = message.get("1", {}) if isinstance(message, dict) else {}
                    if not card_title and isinstance(card_message_1, dict):
                        if "6" in card_message_1 and isinstance(card_message_1["6"], dict):
                            message_6 = card_message_1["6"]
                            if "3" in message_6 and isinstance(message_6["3"], dict):
                                message_6_3 = message_6["3"]
                                if "5" in message_6_3:
                                    try:
                                        card_content = json.loads(message_6_3["5"])
                                        if "dxCard" in card_content and "item" in card_content["dxCard"]:
                                            card_item = card_content["dxCard"]["item"]
                                            if "main" in card_item and "exContent" in card_item["main"]:
                                                ex_content = card_item["main"]["exContent"]
                                                card_title = ex_content.get("title", "")
                                    except (json.JSONDecodeError, KeyError) as e:
                                        logger.warning(f"解析卡片消息失败: {e}")

                    card_message_direction = card_message_1.get('7', 0) if isinstance(card_message_1, dict) else 0
                    card_content_type = 0
                    card_is_system_biz = False
                    try:
                        card_message_6 = card_message_1.get('6', {}) if isinstance(card_message_1, dict) else {}
                        if isinstance(card_message_6, dict):
                            card_message_6_3 = card_message_6.get('3', {})
                            if isinstance(card_message_6_3, dict):
                                card_content_type = card_message_6_3.get('4', 0)
                    except Exception:
                        pass

                    try:
                        card_message_10 = card_message_1.get('10', {}) if isinstance(card_message_1, dict) else {}
                        if isinstance(card_message_10, dict):
                            biz_tag = card_message_10.get('bizTag', '')
                            if biz_tag and ('SECURITY' in biz_tag or 'taskName' in biz_tag or 'taskId' in biz_tag):
                                card_is_system_biz = True
                    except Exception:
                        pass

                    is_system_card_message = card_message_direction == 1 or card_content_type == 6 or card_is_system_biz
                    if not is_system_card_message:
                        logger.warning(
                            f'[{msg_time}] 【{self.account_id}】[{msg_id}] ⚠️ 非系统卡片消息，忽略小刀流程 '
                            f'(direction={card_message_direction}, contentType={card_content_type}, isSystemBiz={card_is_system_biz})'
                        )
                        return

                    waiting_bargain_titles = {"我已小刀，待刀成", "我已小刀,待刀成"}
                    ready_to_ship_titles = {"我已成功小刀，待发货", "我已成功小刀,待发货"}

                    if card_title in waiting_bargain_titles:
                        logger.info(f'[{msg_time}] 【{self.account_id}】【系统】检测到"{card_title}"，执行免拼流程')
                        if not _has_canonical_order_scope():
                            _log_missing_canonical_order_scope(
                                f"跳过小刀免拼流程: title={card_title or 'waiting_bargain'}"
                            )
                            return

                        if not self.is_auto_confirm_enabled():
                            logger.info(f'[{msg_time}] 【{self.account_id}】未启用自动确认发货，跳过自动小刀和自动发货')
                            return

                        if item_id and item_id != "未知商品":
                            try:
                                if not await self._ensure_item_owned_by_current_account(
                                    item_id,
                                    log_prefix=f'[{msg_time}] 【{self.account_id}】'
                                ):
                                    logger.warning(f'[{msg_time}] 【{self.account_id}】❌ 商品 {item_id} 不属于当前账号，跳过免拼发货')
                                    return
                                logger.warning(f'[{msg_time}] 【{self.account_id}】✅ 商品 {item_id} 归属验证通过')
                            except Exception as e:
                                logger.error(f'[{msg_time}] 【{self.account_id}】检查商品归属失败: {self._safe_str(e)}，跳过免拼发货')
                                return

                        order_id = self._extract_order_id(message, message_data)
                        if not order_id:
                            logger.warning(f'[{msg_time}] 【{self.account_id}】❌ 未能提取到订单ID，无法执行免拼发货')
                            return

                        self._mark_order_bargain_flow(
                            order_id,
                            item_id=item_id,
                            buyer_id=send_user_id,
                            context=card_title or 'waiting_bargain',
                        )

                        logger.info(f'[{msg_time}] 【{self.account_id}】延迟2秒后执行免拼发货...')
                        await asyncio.sleep(2)
                        result = await self.auto_freeshipping(order_id, item_id, send_user_id)
                        if result.get('success'):
                            self._mark_order_bargain_flow(
                                order_id,
                                item_id=item_id,
                                buyer_id=send_user_id,
                                apply_configured_price=True,
                                success_detected=True,
                                context=f'{card_title or "waiting_bargain"}_success',
                            )
                            logger.info(f'[{msg_time}] 【{self.account_id}】✅ 自动免拼发货成功')
                            logger.info(f'[{msg_time}] 【{self.account_id}】⏳ 已完成免拼，等待"我已成功小刀，待发货"卡片后再自动发货')
                            return
                        else:
                            logger.warning(f'[{msg_time}] 【{self.account_id}】❌ 自动免拼发货失败: {result.get("error", "未知错误")}')
                            logger.info(f'[{msg_time}] 【{self.account_id}】⏹️ 免拼失败，不执行自动发货')
                            return

                    elif card_title in ready_to_ship_titles:
                        logger.info(f'[{msg_time}] 【{self.account_id}】【系统】检测到"{card_title}"，开始自动发货')
                        if not _has_canonical_order_scope():
                            _log_missing_canonical_order_scope(
                                f"跳过小刀待发货自动发货 title={card_title or 'ready_to_ship'}"
                            )
                            return

                        order_id = self._extract_order_id(message, message_data)
                        if order_id:
                            self._mark_order_bargain_flow(
                                order_id,
                                item_id=item_id,
                                buyer_id=send_user_id,
                                apply_configured_price=True,
                                success_detected=True,
                                context=card_title,
                            )

                        if not self.is_auto_confirm_enabled():
                            logger.info(f'[{msg_time}] 【{self.account_id}】未启用自动确认发货，跳过自动发货')
                            return

                        await self._handle_auto_delivery(
                            websocket, message, send_user_name, send_user_id,
                            item_id, chat_id, msg_time, message_data
                        )
                        logger.info(f'[{msg_time}] 【{self.account_id}】⏹️ 小刀成功待发货卡片处理完成')
                        return
                    else:
                        logger.info(f'[{msg_time}] 【{self.account_id}】收到卡片消息，标题: {card_title or "未知"}')

                except Exception as e:
                    logger.error(f"处理卡片消息异常: {self._safe_str(e)}")

            if send_user_id and send_user_name:
                valid_buyer_nick = self._sanitize_buyer_nick(
                    send_user_name,
                    source="message_sender",
                    message_meta=message_10 if isinstance(message_10, dict) else None,
                    log_prefix=f"【{self.account_id}】[{msg_id}]"
                )
                if valid_buyer_nick and _has_canonical_order_scope():
                    try:
                        from db_manager import db_manager
                        db_manager.update_buyer_nick_by_buyer_id(
                            send_user_id,
                            valid_buyer_nick,
                            account_id=canonical_account_id,
                        )
                    except Exception as e:
                        logger.debug(f"更新买家昵称失败: {self._safe_str(e)}")
                elif valid_buyer_nick:
                    _log_missing_canonical_order_scope(
                        f"跳过买家昵称回写: buyer_id={send_user_id}"
                    )

            if not allow_auto_reply:
                logger.info(
                    f"【{self.account_id}】[{msg_id}] ⏹️ 当前消息不进入自动回复链: "
                    f"route={message_route}, status_signal={order_status_signal or 'none'}"
                )
                return

            await self._schedule_debounced_reply(
                chat_id=chat_id,
                message_data=message_data,
                websocket=websocket,
                send_user_name=send_user_name,
                send_user_id=send_user_id,
                send_message=send_message,
                item_id=item_id,
                msg_time=msg_time,
                dedupe_message_id=dedupe_message_id,
                dedupe_create_time=create_time,
            )

        except Exception as e:
            logger.error(f"【{self.account_id}】[{msg_id}] ❌ 处理消息时发生异常: {self._safe_str(e)}")
            if msg_size > 3000:
                logger.error(f"【{self.account_id}】[{msg_id}] ⚠️⚠️⚠️ 大消息({msg_size}字节)处理异常！")
            logger.warning(f"【{self.account_id}】[{msg_id}] 原始消息: {message_data}")
            import traceback
            logger.error(f"【{self.account_id}】[{msg_id}] 异常堆栈: {traceback.format_exc()}")
        finally:
            logger.info(f"【{self.account_id}】[{msg_id}] 🏁 消息处理完成 ({msg_size}字节)")

    async def main(self):
        try:
            logger.info(f"【{self.account_id}】开始启动XianyuLive主程序...")
            await self.create_session()
            logger.info(f"【{self.account_id}】Session创建完成，开始WebSocket连接循环...")

            while True:
                try:
                    current_account_id = self._canonical_account_id()
                    if not self._is_current_account_enabled():
                        logger.info(f"【{self.account_id}】账号已禁用，停止主循环")
                        break

                    init_auth_state = {}
                    if current_account_id:
                        init_auth_state = self.get_init_auth_failure_state(current_account_id) or {}
                    circuit_until = init_auth_state.get('circuit_until', 0)
                    if circuit_until and time.time() < circuit_until:
                        remaining_seconds = max(1, int(circuit_until - time.time()))
                        self._set_connection_state(ConnectionState.RECONNECTING, f"初始化鉴权冷静期剩余{remaining_seconds}秒")
                        logger.warning(
                            f"【{self.account_id}】初始化鉴权失败熔断中，暂停发起新的WebSocket连接，剩余 {remaining_seconds} 秒"
                        )
                        await self._interruptible_sleep(remaining_seconds)
                        continue

                    headers = self._build_websocket_headers()

                    self._set_connection_state(ConnectionState.CONNECTING, "准备建立WebSocket连接")
                    logger.info(f"【{self.account_id}】WebSocket目标地址: {self.base_url}")

                    async with await self._create_websocket_connection(headers) as websocket:
                        self.ws = websocket
                        logger.info(f"【{self.account_id}】WebSocket连接建立成功，开始初始化...")

                        try:
                            await self.init(websocket)
                            logger.info(f"【{self.account_id}】WebSocket初始化完成！")

                            self._set_connection_state(ConnectionState.CONNECTED, "初始化完成，连接就绪")
                            self.connection_failures = 0
                            self.last_successful_connection = time.time()
                            self._reset_stream_activity_state(self.last_successful_connection)

                            logger.warning(f"【{self.account_id}】准备启动后台任务 - 当前状态: heartbeat={self.heartbeat_task}, token_refresh={self.token_refresh_task}, cleanup={self.cleanup_task}, cookie_refresh={self.cookie_refresh_task}, stream_watchdog={self.stream_watchdog_task}")

                            if self.heartbeat_task:
                                logger.warning(f"【{self.account_id}】检测到旧心跳任务引用，先清理...")
                                self._reset_background_tasks()

                            logger.info(f"【{self.account_id}】启动心跳任务...")
                            self.heartbeat_task = asyncio.create_task(self.heartbeat_loop(websocket))

                            tasks_started = []

                            if not self.token_refresh_task or self.token_refresh_task.done():
                                logger.info(f"【{self.account_id}】启动会话保活任务...")
                                self.token_refresh_task = asyncio.create_task(self.token_refresh_loop())
                                tasks_started.append("会话保活")
                            else:
                                logger.info(f"【{self.account_id}】Token刷新任务已在运行，跳过启动")

                            if not self.cleanup_task or self.cleanup_task.done():
                                logger.info(f"【{self.account_id}】启动暂停记录清理任务...")
                                self.cleanup_task = asyncio.create_task(self.pause_cleanup_loop())
                                tasks_started.append("暂停清理")
                            else:
                                logger.info(f"【{self.account_id}】暂停记录清理任务已在运行，跳过启动")

                            if not self.cookie_refresh_task or self.cookie_refresh_task.done():
                                logger.info(f"【{self.account_id}】启动Cookie刷新任务...")
                                self._prime_cookie_refresh_schedule_on_startup()
                                self.cookie_refresh_task = asyncio.create_task(self.cookie_refresh_loop())
                                tasks_started.append("Cookie刷新")
                            else:
                                logger.info(f"【{self.account_id}】Cookie刷新任务已在运行，跳过启动")

                            if not self.stream_watchdog_task or self.stream_watchdog_task.done():
                                logger.info(f"【{self.account_id}】启动业务流看门狗任务...")
                                self.stream_watchdog_task = asyncio.create_task(self.message_stream_watchdog_loop())
                                tasks_started.append("业务流看门狗")
                            else:
                                logger.info(f"【{self.account_id}】业务流看门狗任务已在运行，跳过启动")

                            if self.message_queue_enabled:
                                await self._start_message_queue_workers()
                                tasks_started.append("消息队列")

                            if tasks_started:
                                logger.info(f"【{self.account_id}】✅ 新启动的任务: {', '.join(tasks_started)}")
                            logger.info(
                                f"【{self.account_id}】✅ 所有后台任务状态: "
                                f"心跳(已启动), "
                                f"会话保活({'运行中' if self.token_refresh_task and not self.token_refresh_task.done() else '已启动'}), "
                                f"暂停清理({'运行中' if self.cleanup_task and not self.cleanup_task.done() else '已启动'}), "
                                f"Cookie刷新({'运行中' if self.cookie_refresh_task and not self.cookie_refresh_task.done() else '已启动'}), "
                                f"业务流看门狗({'运行中' if self.stream_watchdog_task and not self.stream_watchdog_task.done() else '已启动'})"
                            )

                            logger.info(f"【{self.account_id}】开始监听WebSocket消息...")
                            logger.info(f"【{self.account_id}】WebSocket连接状态正常，等待服务器消息...")
                            logger.info(f"【{self.account_id}】准备进入消息循环...")

                            async for message in websocket:
                                try:
                                    message_data = json.loads(message)

                                    msg_id = "unknown"
                                    msg_preview = ""
                                    try:
                                        if isinstance(message_data, dict) and "headers" in message_data:
                                            msg_id = message_data["headers"].get("mid", "unknown")
                                        if isinstance(message_data, dict) and "body" in message_data:
                                            if "syncPushPackage" in message_data["body"]:
                                                msg_preview = "[同步包]"
                                            elif "ack" in str(message_data["body"]).lower():
                                                msg_preview = "[确认]"
                                    except Exception:
                                        pass

                                    logger.info(f"【{self.account_id}】📨 收到消息 [ID:{msg_id}] {msg_preview} {len(message) if message else 0}字节")

                                    if await self.handle_heartbeat_response(message_data):
                                        continue

                                    is_sync_package = self.is_sync_package(message_data)
                                    self._mark_non_heartbeat_message(time.time(), is_sync_package=is_sync_package)

                                    if self.message_queue_enabled and self.message_queue_running:
                                        await self._enqueue_message(message_data, websocket, msg_id)
                                    else:
                                        self._create_tracked_task(self._handle_message_with_semaphore(message_data, websocket, msg_id))

                                except Exception as e:
                                    logger.error(f"处理消息出错: {self._safe_str(e)}")
                                    continue
                        finally:
                            if self.message_queue_enabled and self.message_queue_running:
                                logger.info(f"【{self.account_id}】正在停止消息队列工作协程...")
                                await self._stop_message_queue_workers()

                            if self.ws == websocket:
                                self.ws = None
                                logger.info(f"【{self.account_id}】WebSocket连接已退出，引用已清理")

                except InitAuthError as e:
                    error_msg = self._safe_str(e)
                    self.current_token = None
                    self.connection_failures = 0
                    init_auth_state = {}
                    if current_account_id:
                        init_auth_state = self.record_init_auth_failure(current_account_id, error_msg)
                    self.init_auth_failures = int(init_auth_state.get('count', 0))
                    self._set_connection_state(ConnectionState.RECONNECTING, f"初始化鉴权失败第{self.init_auth_failures}次")
                    logger.error(f"【{self.account_id}】初始化鉴权失败 ({self.init_auth_failures}/{self._init_auth_failure_threshold})")
                    logger.error(f"【{self.account_id}】初始化失败原因: {error_msg}")

                    retry_delay = self._calculate_retry_delay(error_msg)
                    circuit_until = init_auth_state.get('circuit_until', 0)
                    if circuit_until and time.time() < circuit_until:
                        circuit_wait = max(1, int(circuit_until - time.time()))
                        retry_delay = max(retry_delay, circuit_wait)
                        logger.warning(
                            f"【{self.account_id}】初始化鉴权失败已达到阈值，进入冷静期 {circuit_wait} 秒后再重试"
                        )
                    else:
                        logger.warning(f"【{self.account_id}】将在 {retry_delay} 秒后重试初始化鉴权...")

                    self._reset_background_tasks()
                    await self._interruptible_sleep(retry_delay)
                    logger.info(f"【{self.account_id}】初始化鉴权重试等待完成，准备重新建立连接...")
                    continue

                except Exception as e:
                    error_msg = self._safe_str(e)
                    import traceback
                    error_type = type(e).__name__

                    is_connection_closed = (
                        'ConnectionClosedError' in error_type or
                        'ConnectionClosed' in error_type or
                        'no close frame received or sent' in error_msg or
                        'IncompleteReadError' in error_type
                    )

                    if is_connection_closed:
                        logger.warning(f"【{self.account_id}】WebSocket连接已关闭 ({self.connection_failures + 1}/{self.max_connection_failures})")
                        logger.warning(f"【{self.account_id}】关闭原因: {error_msg}")
                    else:
                        self.connection_failures += 1
                    self._set_connection_state(ConnectionState.RECONNECTING, f"第{self.connection_failures}次失败")
                    logger.error(f"【{self.account_id}】WebSocket连接异常 ({self.connection_failures}/{self.max_connection_failures})")
                    logger.error(f"【{self.account_id}】异常类型: {error_type}")
                    logger.error(f"【{self.account_id}】异常信息: {error_msg}")
                    logger.warning(f"【{self.account_id}】异常堆栈:\n{traceback.format_exc()}")

                    if self.ws:
                        try:
                            if hasattr(self.ws, 'close_code') and self.ws.close_code is None:
                                try:
                                    await asyncio.wait_for(self.ws.close(), timeout=2.0)
                                except (asyncio.TimeoutError, Exception):
                                    pass
                        except Exception:
                            pass
                        finally:
                            self.ws = None
                            logger.info(f"【{self.account_id}】WebSocket引用已清理")

                    if is_connection_closed:
                        self.connection_failures += 1
                        self._set_connection_state(ConnectionState.RECONNECTING, f"连接关闭，第{self.connection_failures}次重连")

                    if self.connection_failures >= self.max_connection_failures:
                        self._set_connection_state(ConnectionState.FAILED, f"连续失败{self.max_connection_failures}次")
                        logger.warning(f"【{self.account_id}】连续失败{self.max_connection_failures}次，尝试通过密码登录刷新Cookie...")

                        try:
                            refresh_success = await self._try_password_login_refresh(
                                f"连续失败{self.max_connection_failures}次",
                                ignore_slider_failed_backoff=self._has_recent_slider_success(),
                            )

                            if refresh_success:
                                logger.info(f"【{self.account_id}】✅ 密码登录刷新成功，将重置失败计数并继续重连")
                                self.connection_failures = 0
                                self._set_connection_state(ConnectionState.RECONNECTING, "Cookie已刷新，准备重连")
                                await asyncio.sleep(2)
                                continue
                            else:
                                logger.warning(f"【{self.account_id}】❌ 密码登录刷新失败，将重启实例...")
                        except Exception as refresh_e:
                            logger.error(f"【{self.account_id}】密码登录刷新过程异常: {self._safe_str(refresh_e)}")
                            logger.warning(f"【{self.account_id}】将重启实例...")

                        logger.error(f"【{self.account_id}】准备重启实例...")
                        self.connection_failures = 0

                        logger.info(f"【{self.account_id}】重启前先清理后台任务...")
                        try:
                            await asyncio.wait_for(
                                self._cancel_background_tasks(),
                                timeout=8.0
                            )
                            logger.info(f"【{self.account_id}】后台任务已清理完成")
                        except asyncio.TimeoutError:
                            logger.warning(f"【{self.account_id}】后台任务清理超时，强制继续重启")
                        except Exception as cleanup_e:
                            logger.error(f"【{self.account_id}】后台任务清理失败: {self._safe_str(cleanup_e)}")

                        await self._restart_instance()

                        logger.info(f"【{self.account_id}】重启请求已触发，主程序即将退出，新实例将自动启动")
                        return
                    retry_delay = self._calculate_retry_delay(error_msg)
                    logger.warning(f"【{self.account_id}】将在 {retry_delay} 秒后重试连接...")

                    try:
                        if self.current_token:
                            logger.warning(f"【{self.account_id}】清空当前token，重新连接时将重新获取")
                            self.current_token = None

                        logger.info(f"【{self.account_id}】准备重置后台任务引用（快重连模式）...")
                        self._reset_background_tasks()
                        logger.info(f"【{self.account_id}】后台任务引用已重置，可以立即重连")

                        logger.info(f"【{self.account_id}】开始等待 {retry_delay} 秒...")
                        try:
                            sys.stdout.flush()
                        except Exception:
                            pass

                        chunk_size = 5.0
                        remaining = retry_delay
                        start_time = time.time()

                        while remaining > 0:
                            sleep_time = min(chunk_size, remaining)
                            try:
                                await asyncio.sleep(sleep_time)
                                remaining -= sleep_time
                                elapsed = time.time() - start_time
                                if remaining > 0:
                                    logger.info(f"【{self.account_id}】等待中... 已等待 {elapsed:.1f} 秒，剩余 {remaining:.1f} 秒")
                                    try:
                                        sys.stdout.flush()
                                    except Exception:
                                        pass
                            except asyncio.CancelledError:
                                logger.warning(f"【{self.account_id}】等待期间收到取消信号")
                                raise
                            except Exception as sleep_error:
                                logger.error(f"【{self.account_id}】等待期间发生异常: {self._safe_str(sleep_error)}")
                                logger.warning(f"【{self.account_id}】等待异常堆栈:\n{traceback.format_exc()}")
                                if remaining > 0:
                                    await asyncio.sleep(remaining)
                                break

                        logger.info(f"【{self.account_id}】等待完成（总耗时 {time.time() - start_time:.1f} 秒），准备重新连接...")
                        try:
                            sys.stdout.flush()
                        except Exception:
                            pass

                    except Exception as cleanup_error:
                        logger.error(f"【{self.account_id}】清理过程出错 {self._safe_str(cleanup_error)}")
                        logger.warning(f"【{self.account_id}】清理异常堆栈\n{traceback.format_exc()}")
                        self.heartbeat_task = None
                        self.token_refresh_task = None
                        self.cleanup_task = None
                        self.cookie_refresh_task = None
                        self.stream_watchdog_task = None
                        logger.warning(f"【{self.account_id}】清理失败，已强制重置所有任务引用")
                        logger.info(f"【{self.account_id}】清理失败后开始等待 {retry_delay} 秒...")
                        chunk_size = 5.0
                        remaining = retry_delay
                        start_time = time.time()

                        while remaining > 0:
                            sleep_time = min(chunk_size, remaining)
                            try:
                                await asyncio.sleep(sleep_time)
                                remaining -= sleep_time
                                if remaining > 0:
                                    logger.info(f"【{self.account_id}】清理失败后等待... 剩余 {remaining:.1f} 秒")
                            except asyncio.CancelledError:
                                logger.warning(f"【{self.account_id}】清理失败后等待期间收到取消信号")
                                raise
                            except Exception as sleep_error:
                                logger.error(f"【{self.account_id}】清理失败后等待期间发生异常: {self._safe_str(sleep_error)}")
                                if remaining > 0:
                                    await asyncio.sleep(remaining)
                                break

                        logger.info(f"【{self.account_id}】清理失败后等待完成（时 {time.time() - start_time:.1f} 秒）")

                    logger.info(f"【{self.account_id}】开始新丢轮WebSocket连接尝试...")
                    continue
        finally:
            self._set_connection_state(ConnectionState.CLOSED, "程序退出")

            if self.current_token:
                logger.info(f"【{self.account_id}】程序退出，清空当前token")
                self.current_token = None

            has_pending_tasks = any([
                self.heartbeat_task and not self.heartbeat_task.done(),
                self.token_refresh_task and not self.token_refresh_task.done(),
                self.cleanup_task and not self.cleanup_task.done(),
                self.cookie_refresh_task and not self.cookie_refresh_task.done(),
                self.stream_watchdog_task and not self.stream_watchdog_task.done()
            ])

            if has_pending_tasks:
                logger.info(f"【{self.account_id}】检测到未完成的后台任务，执行清理...")
                try:
                    await asyncio.wait_for(
                        self._cancel_background_tasks(),
                        timeout=10.0
                    )
                except asyncio.TimeoutError:
                    logger.error(f"【{self.account_id}】程序退出时任务取消超时，强制继续")
                except Exception as e:
                    logger.error(f"【{self.account_id}】程序退出时任务取消失败: {self._safe_str(e)}")
                finally:
                    self.heartbeat_task = None
                    self.token_refresh_task = None
                    self.cleanup_task = None
                    self.cookie_refresh_task = None
                    self.stream_watchdog_task = None
            else:
                logger.info(f"【{self.account_id}】所有后台任务已清理完成，跳过重复清理")
                self.heartbeat_task = None
                self.token_refresh_task = None
                self.cleanup_task = None
                self.cookie_refresh_task = None
                self.stream_watchdog_task = None

            if self.background_tasks:
                logger.info(f"【{self.account_id}】等待 {len(self.background_tasks)} 个后台任务完成...")
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*self.background_tasks, return_exceptions=True),
                        timeout=10.0
                        )
                except asyncio.TimeoutError:
                    logger.warning(f"【{self.account_id}】后台任务清理超时，强制继续")

            await self.close_session()

            self._unregister_instance()
            logger.info(f"【{self.account_id}】XianyuLive主程序已完全退出")

    async def get_item_list_info(self, page_number=1, page_size=20, retry_count=0, sync_item_details=False):
        if retry_count >= 4:
            logger.error("获取商品信息失败，重试次数过多")
            return {"error": "获取商品信息失败，重试次数过多"}

        if not self.session:
            await self.create_session()

        params = {
            'jsv': '2.7.2',
            'appKey': '34839810',
            't': str(int(time.time()) * 1000),
            'sign': '',
            'v': '1.0',
            'type': 'originaljson',
            'accountSite': 'xianyu',
            'dataType': 'json',
            'timeout': '20000',
            'api': 'mtop.idle.web.xyh.item.list',
            'sessionOption': 'AutoLoginOnly',
            'spm_cnt': 'a21ybx.im.0.0',
            'spm_pre': 'a21ybx.collection.menu.1.272b5141NafCNK'
        }

        data = {
            'needGroupInfo': False,
            'pageNumber': page_number,
            'pageSize': page_size,
            'groupName': '在售',
            'groupId': '58877261',
            'defaultGroup': True,
            "userId": self.myid
        }

        token = trans_cookies(self.cookies_str).get('_m_h5_tk', '').split('_')[0] if trans_cookies(self.cookies_str).get('_m_h5_tk') else ''

        logger.warning(f"准备获取商品列表，token: {token}")
        if token:
            logger.warning(f"使用cookies中的_m_h5_tk token: {self._mask_secret_value(token, head=6, tail=4)}")
        else:
            logger.warning("cookies中没有找到_m_h5_tk token")

        data_val = json.dumps(data, separators=(',', ':'))
        sign = generate_sign(params['t'], token, data_val)
        params['sign'] = sign

        try:
            async with self.session.post(
                'https://h5api.m.goofish.com/h5/mtop.idle.web.xyh.item.list/1.0/',
                params=params,
                data={'data': data_val}
            ) as response:
                res_json = await response.json()

                if await self._apply_response_cookie_updates(response.headers, "item_list"):
                    logger.warning("已更新Cookie到数据库")

                logger.info(f"商品信息获取响应: {res_json}")

                if res_json.get('ret') and res_json['ret'][0] == 'SUCCESS::调用成功':
                    items_data = res_json.get('data', {})
                    card_list = items_data.get('cardList', [])

                    items_list = []
                    for card in card_list:
                        card_data = card.get('cardData', {})
                        if card_data:
                            item_info = {
                                'id': card_data.get('id', ''),
                                'title': card_data.get('title', ''),
                                'price': card_data.get('priceInfo', {}).get('price', ''),
                                'price_text': card_data.get('priceInfo', {}).get('preText', '') + card_data.get('priceInfo', {}).get('price', ''),
                                'category_id': card_data.get('categoryId', ''),
                                'auction_type': card_data.get('auctionType', ''),
                                'item_status': card_data.get('itemStatus', 0),
                                'detail_url': card_data.get('detailUrl', ''),
                                'pic_info': card_data.get('picInfo', {}),
                                'detail_params': card_data.get('detailParams', {}),
                                'track_params': card_data.get('trackParams', {}),
                                'item_label_data': card_data.get('itemLabelDataVO', {}),
                                'card_type': card.get('cardType', 0)
                            }
                            items_list.append(item_info)

                    logger.info(f"成功获取 {len(items_list)} 个商品")

                    print("\n" + "="*80)
                    print(f"📦 账号 {self.myid} 的商品列表(第{page_number}页，{len(items_list)} 个商品")
                    print("="*80)

                    for i, item in enumerate(items_list, 1):
                        print(f"\n🔸 商品 {i}:")
                        print(f"   商品ID: {item.get('id', 'N/A')}")
                        print(f"   商品标题: {item.get('title', 'N/A')}")
                        print(f"   价格: {item.get('price_text', 'N/A')}")
                        print(f"   分类ID: {item.get('category_id', 'N/A')}")
                        print(f"   商品状态: {item.get('item_status', 'N/A')}")
                        print(f"   拍卖类型: {item.get('auction_type', 'N/A')}")
                        print(f"   详情链接: {item.get('detail_url', 'N/A')}")
                        if item.get('pic_info'):
                            pic_info = item['pic_info']
                            print(f"   图片信息: {pic_info.get('width', 'N/A')}x{pic_info.get('height', 'N/A')}")
                            print(f"   图片链接: {pic_info.get('picUrl', 'N/A')}")
                        print(f"   完整信息: {json.dumps(item, ensure_ascii=False, indent=2)}")

                    print("\n" + "="*80)
                    print("商品列表获取完成")
                    print("="*80)

                    if items_list:
                        saved_count = await self.save_items_list_to_db(
                            items_list,
                            sync_item_details=sync_item_details,
                        )
                        logger.info(f"已将 {saved_count} 个商品信息保存到数据库")

                    return {
                        "success": True,
                        "page_number": page_number,
                        "page_size": page_size,
                        "current_count": len(items_list),
                        "items": items_list,
                        "saved_count": saved_count if items_list else 0,
                        "raw_data": items_data
                    }
                else:
                    error_msg = res_json.get('ret', [''])[0] if res_json.get('ret') else ''
                    if 'FAIL_SYS_TOKEN_EXOIRED' in error_msg or 'token' in error_msg.lower():
                        logger.warning(f"Token失效，准备重试 {error_msg}")
                        await asyncio.sleep(0.5)
                        return await self.get_item_list_info(
                            page_number,
                            page_size,
                            retry_count + 1,
                            sync_item_details=sync_item_details,
                        )
                    else:
                        logger.error(f"获取商品信息失败: {res_json}")
                        return {"error": f"获取商品信息失败: {error_msg}"}

        except Exception as e:
            logger.error(f"商品信息API请求异常: {self._safe_str(e)}")
            await asyncio.sleep(0.5)
            return await self.get_item_list_info(
                page_number,
                page_size,
                retry_count + 1,
                sync_item_details=sync_item_details,
            )

    async def get_all_items(self, page_size=20, max_pages=None, sync_item_details=False):
        all_items = []
        page_number = 1
        total_saved = 0

        logger.info(f"开始获取所有商品信息，每页{page_size}条")

        while True:
            if max_pages and page_number > max_pages:
                logger.info(f"达到最大页数限制{max_pages}，停止获取")
                break

            logger.info(f"正在获取第{page_number} 页...")
            result = await self.get_item_list_info(
                page_number,
                page_size,
                sync_item_details=sync_item_details,
            )

            if not result.get("success"):
                logger.error(f"获取第{page_number} 页失败 {result}")
                break

            current_items = result.get("items", [])
            if not current_items:
                logger.info(f"第{page_number} 页没有数据，获取完成")
                break

            all_items.extend(current_items)
            total_saved += result.get("saved_count", 0)

            logger.info(f"第{page_number} 页获取到 {len(current_items)} 个商品")

            if len(current_items) < page_size:
                logger.info(f"第{page_number} 页商品数({len(current_items)})少于页面大小({page_size})，获取完成")
                break

            page_number += 1

            await asyncio.sleep(1)

        logger.info(f"所有商品获取完成，共 {len(all_items)} 个商品，保存了 {total_saved} 条")

        return {
            "success": True,
            "total_pages": page_number,
            "total_count": len(all_items),
            "total_saved": total_saved,
            "items": all_items
        }

    def _get_item_polish_module(self):
        if os.getenv('ITEM_POLISH_IMPL', '').strip().lower() == 'plain':
            from item_polish_module import ItemPolishModule
        else:
            from secure_item_polish_ultra import ItemPolishModule

        return ItemPolishModule(self)

    async def polish_item(self, item_id, retry_count=0):
        return await self._get_item_polish_module().polish_item(item_id, retry_count)

    async def _polish_item_backup(self, item_id):
        return await self._get_item_polish_module()._polish_item_backup(item_id)

    async def polish_all_items(self):
        return await self._get_item_polish_module().polish_all_items()

    async def send_image_msg(self, ws, cid, toid, image_url, width=800, height=600, card_id=None):
        try:
            original_url = image_url

            if self._is_cdn_url(image_url):
                logger.info(f"【{self.account_id}】使用已有的CDN图片链接: {image_url}")
            elif image_url.startswith('/static/uploads/') or image_url.startswith('static/uploads/'):
                local_image_path = image_url.replace('/static/uploads/', 'static/uploads/')
                if os.path.exists(local_image_path):
                    logger.info(f"【{self.account_id}】准备上传本地图片到闲鱼CDN: {local_image_path}")

                    from utils.image_uploader import ImageUploader
                    uploader = ImageUploader(self.cookies_str)

                    async with uploader:
                        cdn_url = await uploader.upload_image(local_image_path)
                        if cdn_url:
                            logger.info(f"【{self.account_id}】图片上传成功，CDN URL: {cdn_url}")
                            image_url = cdn_url

                            if card_id is not None:
                                await self._update_card_image_url(card_id, cdn_url)

                            from utils.image_utils import image_manager
                            try:
                                actual_width, actual_height = image_manager.get_image_size(local_image_path)
                                if actual_width and actual_height:
                                    width, height = actual_width, actual_height
                                    logger.info(f"【{self.account_id}】获取到实际图片尺寸: {width}x{height}")
                            except Exception as e:
                                logger.warning(f"【{self.account_id}】获取图片尺寸失败，使用默认尺寸: {e}")
                        else:
                            logger.error(f"【{self.account_id}】图片上传失败 {local_image_path}")
                            logger.error(f"【{self.account_id}】❌ Cookie可能已失效！请检查配置并更新Cookie")
                            raise Exception(f"图片上传失败（Cookie可能已失效）: {local_image_path}")
                else:
                    logger.error(f"【{self.account_id}】本地图片文件不存在: {local_image_path}")
                    raise Exception(f"本地图片文件不存在 {local_image_path}")
            else:
                logger.warning(f"【{self.account_id}】未知的图片URL格式: {image_url}")

            logger.info(f"【{self.account_id}】准备发送图片消息")
            logger.info(f"  - 原始URL: {original_url}")
            logger.info(f"  - CDN URL: {image_url}")
            logger.info(f"  - 图片尺寸: {width}x{height}")
            logger.info(f"  - 聊天ID: {cid}")
            logger.info(f"  - 接收者ID: {toid}")

            image_content = {
                "contentType": 2,
                "image": {
                    "pics": [
                        {
                            "height": int(height),
                            "type": 0,
                            "url": image_url,
                            "width": int(width)
                        }
                    ]
                }
            }

            content_json = json.dumps(image_content, ensure_ascii=False)
            content_base64 = str(base64.b64encode(content_json.encode('utf-8')), 'utf-8')

            logger.info(f"【{self.account_id}】图片内容JSON: {content_json}")
            logger.info(f"【{self.account_id}】Base64编码长度: {len(content_base64)}")

            msg = {
                "lwp": "/r/MessageSend/sendByReceiverScope",
                "headers": {
                    "mid": generate_mid()
                },
                "body": [
                    {
                        "uuid": generate_uuid(),
                        "cid": f"{cid}@goofish",
                        "conversationType": 1,
                        "content": {
                            "contentType": 101,
                            "custom": {
                                "type": 1,
                                "data": content_base64
                            }
                        },
                        "redPointPolicy": 0,
                        "extension": {
                            "extJson": "{}"
                        },
                        "ctx": {
                            "appVersion": "1.0",
                            "platform": "web"
                        },
                        "mtags": {},
                        "msgReadStatusSetting": 1
                    },
                    {
                        "actualReceivers": [
                            f"{toid}@goofish",
                            f"{self.myid}@goofish"
                        ]
                    }
                ]
            }

            await ws.send(json.dumps(msg))
            logger.info(f"【{self.account_id}】图片消息发送成功 {image_url}")

        except Exception as e:
            logger.error(f"【{self.account_id}】发送图片消息失败 {self._safe_str(e)}")
            raise

    async def send_image_from_file(self, ws, cid, toid, image_path):
        try:
            logger.info(f"【{self.account_id}】开始上传图片 {image_path}")

            from utils.image_uploader import ImageUploader
            uploader = ImageUploader(self.cookies_str)

            async with uploader:
                image_url = await uploader.upload_image(image_path)

            if image_url:
                from utils.image_utils import image_manager
                try:
                    from PIL import Image
                    with Image.open(image_path) as img:
                        width, height = img.size
                except Exception as e:
                    logger.warning(f"无法获取图片尺寸，使用默认值: {e}")
                    width, height = 800, 600

                await self.send_image_msg(ws, cid, toid, image_url, width, height)
                logger.info(f"【{self.account_id}】图片发送完成 {image_path} -> {image_url}")
                return True
            else:
                logger.error(f"【{self.account_id}】图片上传失败 {image_path}")
                logger.error(f"【{self.account_id}】❌ Cookie可能已失效！请检查配置并更新Cookie")
                return False

        except Exception as e:
            logger.error(f"【{self.account_id}】从文件发图片失败 {self._safe_str(e)}")
            return False

if __name__ == '__main__':
    cookies_str = os.getenv('COOKIES_STR')
    account_id = os.getenv('ACCOUNT_ID')
    xianyuLive = XianyuLive(cookies_str, account_id=account_id)
    asyncio.run(xianyuLive.main())
