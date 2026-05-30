import sqlite3
import os
import threading
import hashlib
import time
import json
import random
import string
import re
import aiohttp
import io
import base64
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageDraw, ImageFont
from typing import List, Tuple, Dict, Optional, Any
from urllib.parse import parse_qs, urlparse
from cryptography.fernet import Fernet, InvalidToken
from loguru import logger

ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

class DBManager:
    ADMIN_DATA_HIDDEN_SYSTEM_SETTING_KEYS = {
        "admin_password_hash",
        "smtp_password",
        "qq_reply_secret_key",
    }

    """SQLite数据库管理，持久化存储Cookie和关键字"""
    
    def __init__(self, db_path: str = None):
        """初始化数据库连接和表结构"""
        # 支持环境变量配置数据库路径
        if db_path is None:
            db_path = os.getenv('DB_PATH', 'data/xianyu_data.db')

        # 确保数据目录存在并有正确权限
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            try:
                os.makedirs(db_dir, mode=0o755, exist_ok=True)
                logger.info(f"创建数据目录: {db_dir}")
            except PermissionError as e:
                logger.error(f"创建数据目录失败，权限不足: {e}")
                # 尝试使用当前目录
                db_path = os.path.basename(db_path)
                logger.warning(f"使用当前目录作为数据库路径: {db_path}")
            except Exception as e:
                logger.error(f"创建数据目录失败: {e}")
                raise

        # 检查目录权限
        if db_dir and os.path.exists(db_dir):
            if not os.access(db_dir, os.W_OK):
                logger.error(f"数据目录没有写权限: {db_dir}")
                # 尝试使用当前目录
                db_path = os.path.basename(db_path)
                logger.warning(f"使用当前目录作为数据库路径: {db_path}")

        self.db_path = db_path
        logger.info(f"数据库路径: {self.db_path}")
        self.conn = None
        self.lock = threading.RLock()  # 使用可重入锁保护数据库操作
        self.secret_fernet = None
        self.secret_key_path = None

        # SQL日志配置 - 默认启用
        self.sql_log_enabled = True  # 默认启用SQL日志
        self.sql_log_level = 'INFO'  # 默认使用INFO级别

        # 允许通过环境变量覆盖默认设置
        if os.getenv('SQL_LOG_ENABLED'):
            self.sql_log_enabled = os.getenv('SQL_LOG_ENABLED', 'true').lower() == 'true'
        if os.getenv('SQL_LOG_LEVEL'):
            self.sql_log_level = os.getenv('SQL_LOG_LEVEL', 'INFO').upper()

        logger.info(f"SQL日志已启用，日志级别: {self.sql_log_level}")

        self._init_secret_cipher()

        self.init_db()
        try:
            self.recover_stale_batch_data_reservations()
        except Exception as e:
            logger.warning(f"恢复过期批量数据预占失败: {e}")
        try:
            self._migrate_plaintext_cookie_secrets()
        except Exception as e:
            logger.warning(f"迁移明文账号敏感信息失败: {e}")

    def _configure_connection(self) -> None:
        """统一配置 SQLite 连接级行为。"""
        if self.conn is None:
            return

        # SQLite 默认不启用外键约束；管理台删除/清空强依赖级联约束清理关联表。
        self.conn.execute("PRAGMA foreign_keys = ON")

    def _init_secret_cipher(self):
        """初始化敏感字段加密器。"""
        env_key = os.getenv('SECRET_ENCRYPTION_KEY', '').strip()
        if env_key:
            key = env_key.encode('utf-8')
        else:
            db_dir = os.path.dirname(self.db_path) or '.'
            self.secret_key_path = os.path.join(db_dir, '.secret_encryption.key')
            if os.path.exists(self.secret_key_path):
                with open(self.secret_key_path, 'rb') as f:
                    key = f.read().strip()
            else:
                key = Fernet.generate_key()
                with open(self.secret_key_path, 'wb') as f:
                    f.write(key)
                try:
                    os.chmod(self.secret_key_path, 0o600)
                except Exception:
                    pass

        self.secret_fernet = Fernet(key)

    def _is_encrypted_secret(self, value: Any) -> bool:
        return isinstance(value, str) and value.startswith('enc$')

    def _encrypt_secret(self, value: Any) -> Any:
        if value is None:
            return None
        text = str(value)
        if text == '':
            return ''
        if self._is_encrypted_secret(text):
            return text
        token = self.secret_fernet.encrypt(text.encode('utf-8')).decode('utf-8')
        return f'enc${token}'

    def _decrypt_secret(self, value: Any) -> str:
        if value in (None, ''):
            return ''
        text = str(value)
        if not self._is_encrypted_secret(text):
            return text
        try:
            return self.secret_fernet.decrypt(text[4:].encode('utf-8')).decode('utf-8')
        except InvalidToken:
            logger.warning("检测到无法解密的敏感字段，按原值返回")
            return text

    def _migrate_plaintext_cookie_secrets(self):
        """将 cookies 表中的明文敏感字段迁移为密文存储。"""
        with self.lock:
            cursor = self.conn.cursor()
            self._execute_sql(cursor, "SELECT id, value, password, proxy_pass FROM cookies")
            rows = cursor.fetchall()
            updated_count = 0

            for account_id, cookie_value, password, proxy_pass in rows:
                update_fields = []
                params = []

                if cookie_value and not self._is_encrypted_secret(cookie_value):
                    update_fields.append("value = ?")
                    params.append(self._encrypt_secret(cookie_value))

                if password and not self._is_encrypted_secret(password):
                    update_fields.append("password = ?")
                    params.append(self._encrypt_secret(password))

                if proxy_pass and not self._is_encrypted_secret(proxy_pass):
                    update_fields.append("proxy_pass = ?")
                    params.append(self._encrypt_secret(proxy_pass))

                if not update_fields:
                    continue

                params.append(account_id)
                self._execute_sql(cursor, f"UPDATE cookies SET {', '.join(update_fields)} WHERE id = ?", tuple(params))
                updated_count += 1

            if updated_count:
                self.conn.commit()
                logger.info(f"已迁移 {updated_count} 条 cookies 敏感字段为密文存储")

    def _normalize_order_status(self, status: str) -> str:
        """标准化订单状态，统一为系统内部状态值。"""
        if status is None:
            return None

        normalized = str(status).strip().lower()
        if not normalized:
            return None

        status_map = {
            # 内部标准状态
            'processing': 'processing',
            'pending_payment': 'pending_payment',
            'pending_ship': 'pending_ship',
            'pending_delivery': 'pending_ship',
            'partial_success': 'partial_success',
            'partial_pending_finalize': 'partial_pending_finalize',
            'shipped': 'shipped',
            'completed': 'completed',
            'refunding': 'refunding',
            'refund_cancelled': 'refund_cancelled',
            'cancelled': 'cancelled',
            'unknown': 'unknown',
            # 常见外部/历史状态兼容
            'success': 'completed',
            'refunded': 'cancelled',
            'closed': 'cancelled',
            'canceled': 'cancelled',
            'delivered': 'shipped',
            # 中文状态兼容
            '处理中': 'processing',
            '待发货': 'pending_ship',
            '部分发货': 'partial_success',
            '部分待收尾': 'partial_pending_finalize',
            '已发货': 'shipped',
            '已完成': 'completed',
            '退款中': 'refunding',
            '退款撤销': 'refund_cancelled',
            '已关闭': 'cancelled',
        }

        mapped = status_map.get(normalized, normalized)
        if mapped != normalized:
            logger.info(f"标准化订单状态: {status} -> {mapped}")
        elif normalized not in {
            'processing', 'pending_payment', 'pending_ship', 'partial_success', 'partial_pending_finalize', 'shipped', 'completed',
            'refunding', 'refund_cancelled', 'cancelled', 'unknown'
        }:
            logger.warning(f"检测到未映射订单状态，按原值保存: {status}")
        return mapped

    def _get_order_status_priority(self, status: str) -> int:
        normalized = self._normalize_order_status(status)
        priority_map = {
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
        return priority_map.get(normalized, 0)

    def resolve_external_order_status(self, current_status: str, incoming_status: str, source: str = "external_sync") -> str:
        """合并外部/旁路状态写入，避免更粗粒度状态覆盖内部进度状态。"""
        normalized_current = self._normalize_order_status(current_status)
        normalized_incoming = self._normalize_order_status(incoming_status)

        if not normalized_incoming or normalized_incoming == 'unknown':
            return None

        if not normalized_current or normalized_current == 'unknown':
            return normalized_incoming

        blocked_incoming_map = {
            'pending_payment': {'processing'},
            'pending_ship': {'processing', 'pending_payment'},
            'partial_success': {'processing', 'pending_payment', 'pending_ship', 'shipped'},
            'partial_pending_finalize': {'processing', 'pending_payment', 'pending_ship', 'shipped'},
            'shipped': {'processing', 'pending_payment', 'pending_ship'},
            'completed': {'processing', 'pending_payment', 'pending_ship', 'partial_success', 'partial_pending_finalize', 'shipped'},
            'refunding': {'processing', 'pending_payment', 'pending_ship', 'partial_success', 'partial_pending_finalize', 'shipped'},
            'cancelled': {'processing', 'pending_payment', 'pending_ship', 'partial_success', 'partial_pending_finalize', 'shipped', 'completed', 'refunding'},
        }

        blocked_incoming = blocked_incoming_map.get(normalized_current, set())
        if normalized_incoming in blocked_incoming:
            logger.warning(
                f"忽略外部订单状态覆盖: source={source}, current={normalized_current}, incoming={normalized_incoming}"
            )
            return normalized_current

        current_priority = self._get_order_status_priority(normalized_current)
        incoming_priority = self._get_order_status_priority(normalized_incoming)
        if (
            current_priority
            and incoming_priority
            and incoming_priority < current_priority
            and normalized_incoming not in {'refunding', 'cancelled', 'refund_cancelled'}
        ):
            logger.warning(
                f"忽略低优先级外部状态覆盖: source={source}, current={normalized_current}, incoming={normalized_incoming}"
            )
            return normalized_current

        return normalized_incoming
    
    def init_db(self):
        """初始化数据库表结构"""
        try:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._configure_connection()
            cursor = self.conn.cursor()
            
            # 创建用户表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')

            # 创建邮箱验证码表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS email_verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                code TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                used BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')

            # 创建图形验证码表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS captcha_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                code TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')

            # 创建cookies表（添加user_id字段和auto_confirm字段）
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS cookies (
                id TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                auto_confirm INTEGER DEFAULT 1,
                bound_unb TEXT DEFAULT '',
                bind_status TEXT DEFAULT 'active',
                remark TEXT DEFAULT '',
                pause_duration INTEGER DEFAULT 10,
                username TEXT DEFAULT '',
                password TEXT DEFAULT '',
                show_browser INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            ''')

            
            # 创建keywords表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS keywords (
                account_id TEXT,
                keyword TEXT,
                reply TEXT,
                item_id TEXT,
                type TEXT DEFAULT 'text',
                image_url TEXT,
                FOREIGN KEY (account_id) REFERENCES cookies(id) ON DELETE CASCADE
            )
            ''')

            # 创建cookie_status表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS cookie_status (
                account_id TEXT PRIMARY KEY,
                enabled BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES cookies(id) ON DELETE CASCADE
            )
            ''')

            # 创建AI回复配置表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_reply_settings (
                account_id TEXT PRIMARY KEY,
                ai_enabled BOOLEAN DEFAULT FALSE,
                model_name TEXT DEFAULT 'qwen-plus',
                api_key TEXT,
                base_url TEXT DEFAULT 'https://dashscope.aliyuncs.com/compatible-mode/v1',
                api_type TEXT DEFAULT '',
                max_discount_percent INTEGER DEFAULT 10,
                max_discount_amount INTEGER DEFAULT 100,
                max_bargain_rounds INTEGER DEFAULT 3,
                custom_prompts TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES cookies(id) ON DELETE CASCADE
            )
            ''')

            # 创建AI配置预设表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_config_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                preset_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                api_key TEXT NOT NULL DEFAULT '',
                base_url TEXT NOT NULL DEFAULT '',
                api_type TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, preset_name)
            )
            ''')

            # 创建AI对话历史表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                intent TEXT,
                bargain_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES cookies (id) ON DELETE CASCADE
            )
            ''')

            # 创建AI商品信息缓存表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_item_cache (
                item_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                price REAL,
                description TEXT,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')

            # 创建卡券表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL CHECK (type IN ('api', 'yifan_api', 'text', 'data', 'image')),
                api_config TEXT,
                text_content TEXT,
                data_content TEXT,
                image_url TEXT,
                description TEXT,
                enabled BOOLEAN DEFAULT TRUE,
                delay_seconds INTEGER DEFAULT 0,
                is_multi_spec BOOLEAN DEFAULT FALSE,
                spec_name TEXT,
                spec_value TEXT,
                spec_name_2 TEXT,
                spec_value_2 TEXT,
                user_id INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
            ''')

            # 创建订单表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                item_id TEXT,
                buyer_id TEXT,
                buyer_nick TEXT,
                sid TEXT,
                spec_name TEXT,
                spec_value TEXT,
                spec_name_2 TEXT,
                spec_value_2 TEXT,
                quantity TEXT,
                amount TEXT,
                bargain_flow_detected INTEGER DEFAULT 0,
                bargain_success_detected INTEGER DEFAULT 0,
                order_status TEXT DEFAULT 'unknown',
                pre_refund_status TEXT,
                platform_created_at TIMESTAMP,
                platform_paid_at TIMESTAMP,
                platform_completed_at TIMESTAMP,
                account_id TEXT,
                yifan_orderno TEXT,
                delivery_status TEXT,
                callback_data TEXT,
                chat_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES cookies(id) ON DELETE CASCADE
            )
            ''')
            
            # 检查并添加 sid 列到 orders 表（用于简化消息查找订单）
            try:
                self._execute_sql(cursor, "SELECT sid FROM orders LIMIT 1")
            except sqlite3.OperationalError:
                # sid 列不存在，需要添加
                logger.info("正在为 orders 表添加 sid 列...")
                self._execute_sql(cursor, "ALTER TABLE orders ADD COLUMN sid TEXT")
                self._execute_sql(cursor, "CREATE INDEX IF NOT EXISTS idx_orders_sid ON orders(sid)")
                logger.info("orders 表 sid 列添加完成")

            # 检查并添加 buyer_nick 列到 orders 表（用于存储买家昵称）
            try:
                self._execute_sql(cursor, "SELECT buyer_nick FROM orders LIMIT 1")
            except sqlite3.OperationalError:
                # buyer_nick 列不存在，需要添加
                logger.info("正在为 orders 表添加 buyer_nick 列...")
                self._execute_sql(cursor, "ALTER TABLE orders ADD COLUMN buyer_nick TEXT")
                logger.info("orders 表 buyer_nick 列添加完成")

            # 检查并添加 pre_refund_status 列到 orders 表（用于退款撤销跨重启回退）
            try:
                self._execute_sql(cursor, "SELECT pre_refund_status FROM orders LIMIT 1")
            except sqlite3.OperationalError:
                logger.info("正在为 orders 表添加 pre_refund_status 列...")
                self._execute_sql(cursor, "ALTER TABLE orders ADD COLUMN pre_refund_status TEXT")
                logger.info("orders 表 pre_refund_status 列添加完成")

            # 检查并添加 bargain_flow_detected 列（用于记录小刀/拼团成交价覆盖）
            try:
                self._execute_sql(cursor, "SELECT bargain_flow_detected FROM orders LIMIT 1")
            except sqlite3.OperationalError:
                logger.info("正在为 orders 表添加 bargain_flow_detected 列...")
                self._execute_sql(cursor, "ALTER TABLE orders ADD COLUMN bargain_flow_detected INTEGER DEFAULT 0")
                logger.info("orders 表 bargain_flow_detected 列添加完成")

            # 检查并添加 bargain_success_detected 列（用于记录小刀已进入第二阶段的成功证据）
            try:
                self._execute_sql(cursor, "SELECT bargain_success_detected FROM orders LIMIT 1")
            except sqlite3.OperationalError:
                logger.info("正在为 orders 表添加 bargain_success_detected 列...")
                self._execute_sql(cursor, "ALTER TABLE orders ADD COLUMN bargain_success_detected INTEGER DEFAULT 0")
                logger.info("orders 表 bargain_success_detected 列添加完成")

            # 检查并添加 user_id 列（用于数据库迁移）
            try:
                self._execute_sql(cursor, "SELECT user_id FROM cards LIMIT 1")
            except sqlite3.OperationalError:
                # user_id 列不存在，需要添加
                logger.info("正在为 cards 表添加 user_id 列...")
                self._execute_sql(cursor, "ALTER TABLE cards ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
                self._execute_sql(cursor, "CREATE INDEX IF NOT EXISTS idx_cards_user_id ON cards(user_id)")
                logger.info("cards 表 user_id 列添加完成")

            # 检查并添加 delay_seconds 列（用于自动发货延时功能）
            try:
                self._execute_sql(cursor, "SELECT delay_seconds FROM cards LIMIT 1")
            except sqlite3.OperationalError:
                # delay_seconds 列不存在，需要添加
                logger.info("正在为 cards 表添加 delay_seconds 列...")
                self._execute_sql(cursor, "ALTER TABLE cards ADD COLUMN delay_seconds INTEGER DEFAULT 0")
                logger.info("cards 表 delay_seconds 列添加完成")

            # 检查并添加 item_id 列（用于自动回复商品ID功能）
            try:
                self._execute_sql(cursor, "SELECT item_id FROM keywords LIMIT 1")
            except sqlite3.OperationalError:
                # item_id 列不存在，需要添加
                logger.info("正在为 keywords 表添加 item_id 列...")
                self._execute_sql(cursor, "ALTER TABLE keywords ADD COLUMN item_id TEXT")
                logger.info("keywords 表 item_id 列添加完成")

            # 创建商品信息表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS item_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                item_title TEXT,
                item_description TEXT,
                item_category TEXT,
                item_price TEXT,
                item_detail TEXT,
                is_multi_spec BOOLEAN DEFAULT FALSE,
                multi_quantity_delivery BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES cookies(id) ON DELETE CASCADE,
                UNIQUE(account_id, item_id)
            )
            ''')

            # 检查并添加 multi_quantity_delivery 列（用于多数量发货功能）
            try:
                self._execute_sql(cursor, "SELECT multi_quantity_delivery FROM item_info LIMIT 1")
            except sqlite3.OperationalError:
                # multi_quantity_delivery 列不存在，需要添加
                logger.info("正在为 item_info 表添加 multi_quantity_delivery 列...")
                self._execute_sql(cursor, "ALTER TABLE item_info ADD COLUMN multi_quantity_delivery BOOLEAN DEFAULT FALSE")
                logger.info("item_info 表 multi_quantity_delivery 列添加完成")

            # 创建自动发货规则表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS delivery_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                card_id INTEGER NOT NULL,
                delivery_count INTEGER DEFAULT 1,
                enabled BOOLEAN DEFAULT TRUE,
                description TEXT,
                delivery_times INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE
            )
            ''')

            # 创建发货日志表（记录真实发货尝试结果：成功/失败）
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS delivery_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL DEFAULT 1,
                account_id TEXT,
                order_id TEXT,
                item_id TEXT,
                buyer_id TEXT,
                buyer_nick TEXT,
                rule_id INTEGER,
                rule_keyword TEXT,
                card_type TEXT,
                match_mode TEXT,
                channel TEXT NOT NULL DEFAULT 'auto',
                status TEXT NOT NULL DEFAULT 'failed',
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (account_id) REFERENCES cookies(id) ON DELETE SET NULL,
                FOREIGN KEY (rule_id) REFERENCES delivery_rules(id) ON DELETE SET NULL
            )
            ''')
            self._execute_sql(cursor, "CREATE INDEX IF NOT EXISTS idx_delivery_logs_user_time ON delivery_logs(user_id, created_at)")
            self._execute_sql(cursor, "CREATE INDEX IF NOT EXISTS idx_delivery_logs_order_id ON delivery_logs(order_id)")

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS delivery_finalization_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                unit_index INTEGER NOT NULL DEFAULT 1,
                account_id TEXT,
                item_id TEXT,
                buyer_id TEXT,
                channel TEXT NOT NULL DEFAULT 'auto',
                status TEXT NOT NULL DEFAULT 'sent',
                delivery_meta TEXT,
                last_error TEXT,
                sent_at TIMESTAMP,
                finalized_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(order_id, unit_index)
            )
            ''')
            self._execute_sql(cursor, "CREATE INDEX IF NOT EXISTS idx_delivery_finalization_states_status ON delivery_finalization_states(status, updated_at)")
            self._execute_sql(cursor, "CREATE INDEX IF NOT EXISTS idx_delivery_finalization_states_account_id ON delivery_finalization_states(account_id)")

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS data_card_reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL,
                order_id TEXT NOT NULL,
                account_id TEXT,
                buyer_id TEXT,
                unit_index INTEGER NOT NULL DEFAULT 1,
                reserved_content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'reserved',
                last_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMP,
                finalized_at TIMESTAMP,
                released_at TIMESTAMP,
                expires_at TIMESTAMP,
                FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE
            )
            ''')
            self._execute_sql(cursor, "CREATE INDEX IF NOT EXISTS idx_data_card_reservations_card_status ON data_card_reservations(card_id, status)")
            self._execute_sql(cursor, "CREATE INDEX IF NOT EXISTS idx_data_card_reservations_order_status ON data_card_reservations(order_id, status)")
            self._execute_sql(cursor, "CREATE INDEX IF NOT EXISTS idx_data_card_reservations_card_order_unit ON data_card_reservations(card_id, order_id, unit_index)")

            # 创建默认回复表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS default_replies (
                account_id TEXT PRIMARY KEY,
                enabled BOOLEAN DEFAULT FALSE,
                reply_content TEXT,
                reply_once BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES cookies(id) ON DELETE CASCADE
            )
            ''')

            # 添加 reply_once 字段（如果不存在）
            try:
                cursor.execute('ALTER TABLE default_replies ADD COLUMN reply_once BOOLEAN DEFAULT FALSE')
                self.conn.commit()
                logger.info("已添加 reply_once 字段到 default_replies 表")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    logger.warning(f"添加 reply_once 字段失败: {e}")

            # 创建指定商品回复表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS item_replay (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    reply_content TEXT NOT NULL ,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(account_id, item_id)
                )
            ''')

            # 创建默认回复记录表（记录已回复的chat_id）
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS default_reply_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                replied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(account_id, chat_id),
                FOREIGN KEY (account_id) REFERENCES cookies(id) ON DELETE CASCADE
            )
            ''')

            # 创建通知渠道表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS notification_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL CHECK (type IN ('qq','ding_talk','dingtalk','feishu','lark','bark','email','webhook','wechat','telegram')),
                config TEXT NOT NULL,
                enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')

            # 创建系统设置表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                description TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')

            # 创建消息通知配置表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS message_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES cookies(id) ON DELETE CASCADE,
                FOREIGN KEY (channel_id) REFERENCES notification_channels(id) ON DELETE CASCADE,
                UNIQUE(account_id, channel_id)
            )
            ''')

            # 创建用户设置表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, key)
            )
            ''')

            # 创建好评模板表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS comment_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                is_active BOOLEAN DEFAULT FALSE,
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES cookies(id) ON DELETE CASCADE
            )
            ''')

            # 创建风控日志表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS risk_control_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
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
                FOREIGN KEY (account_id) REFERENCES cookies(id) ON DELETE CASCADE
            )
            ''')

            # 创建通知模板表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS notification_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK (type IN ('message', 'token_refresh', 'delivery', 'slider_success', 'face_verify', 'password_login_success', 'cookie_refresh_success')),
                template TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, type),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            ''')

            # 创建定时任务表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                task_type TEXT NOT NULL DEFAULT 'item_polish',
                account_id TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                interval_hours INTEGER DEFAULT 24,
                delay_minutes INTEGER DEFAULT 0,
                random_delay_max INTEGER DEFAULT 10,
                next_run_at TEXT,
                last_run_at TEXT,
                last_run_result TEXT,
                user_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES cookies(id) ON DELETE CASCADE
            )
            ''')

            # 通知模板默认值在 _migrate_notification_templates 中按 user_id 维度补齐。

            # 插入默认系统设置（不包括管理员密码，由reply_server.py初始化）
            cursor.execute('''
            INSERT OR IGNORE INTO system_settings (key, value, description) VALUES
            ('theme_color', '#4f46e5', '主题颜色'),
            ('registration_enabled', 'true', '是否开启用户注册'),
            ('show_default_login_info', 'true', '是否显示默认登录信息'),
            ('login_captcha_enabled', 'true', '是否开启登录验证码'),
            ('smtp_server', '', 'SMTP服务器地址'),
            ('smtp_port', '587', 'SMTP端口'),
            ('smtp_user', '', 'SMTP登录用户名（发件邮箱）'),
            ('smtp_password', '', 'SMTP登录密码/授权码'),
            ('smtp_from', '', '发件人显示名（留空则使用邮箱地址）'),
            ('smtp_use_tls', 'true', '是否启用TLS'),
            ('smtp_use_ssl', 'false', '是否启用SSL'),
            ('verification_email_api_url', '', '验证码邮件API地址（留空则仅使用SMTP）'),
            ('qq_notification_api_url', '', 'QQ通知API地址（留空则禁用QQ通知）'),
            ('auto_comment_api_url', '', '自动好评辅助API地址（留空则禁用外部辅助）'),
            ('qq_reply_secret_key', 'xianyu_qq_reply_2024', 'QQ回复消息API秘钥')
            ''')

            # 检查并升级数据库
            self.check_and_upgrade_db(cursor)

            # 执行数据库迁移
            self._migrate_database(cursor)
            self._ensure_cookie_binding_columns(cursor)

            self.conn.commit()
            logger.info("数据库初始化完成")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")
            self.conn.rollback()
            raise

    def _ensure_cookie_binding_columns(self, cursor):
        """显式保障 cookies 表中的账号绑定字段存在。"""
        required_columns = {
            'bound_unb': "TEXT DEFAULT ''",
            'bind_status': "TEXT DEFAULT 'active'",
        }

        self._execute_sql(cursor, "PRAGMA table_info(cookies)")
        existing_columns = {column[1] for column in cursor.fetchall()}

        for column_name, column_definition in required_columns.items():
            if column_name in existing_columns:
                continue

            logger.warning(f"检测到 cookies 表缺少 {column_name} 列，开始补齐")
            self._execute_sql(
                cursor,
                f"ALTER TABLE cookies ADD COLUMN {column_name} {column_definition}",
            )
            existing_columns.add(column_name)

        self._execute_sql(cursor, "PRAGMA table_info(cookies)")
        final_columns = {column[1] for column in cursor.fetchall()}
        missing_columns = [
            column_name
            for column_name in required_columns
            if column_name not in final_columns
        ]
        if missing_columns:
            raise sqlite3.OperationalError(
                f"cookies 表缺少必需绑定字段: {', '.join(missing_columns)}"
            )

    def _ensure_risk_control_logs_account_schema(self, cursor):
        """将 risk_control_logs 的遗留账号关联结构重建为 account_id 作用域。"""
        legacy_link_column = "".join(["cookie", "_id"])

        self._execute_sql(cursor, "PRAGMA table_info(risk_control_logs)")
        risk_log_columns = [column[1] for column in cursor.fetchall()]
        if legacy_link_column not in risk_log_columns:
            return risk_log_columns

        logger.warning("检测到 risk_control_logs 仍使用遗留账号关联结构，开始重建为 account_id 作用域")
        account_id_expr = (
            f"COALESCE(NULLIF(TRIM(account_id), ''), NULLIF(TRIM({legacy_link_column}), ''))"
            if "account_id" in risk_log_columns
            else f"NULLIF(TRIM({legacy_link_column}), '')"
        )

        self._execute_sql(cursor, "DROP TABLE IF EXISTS risk_control_logs_new")
        self._execute_sql(
            cursor,
            """
            CREATE TABLE risk_control_logs_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
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
                FOREIGN KEY (account_id) REFERENCES cookies(id) ON DELETE CASCADE
            )
            """,
        )
        self._execute_sql(
            cursor,
            f"""
            INSERT INTO risk_control_logs_new (
                id, account_id, event_type, session_id, trigger_scene, result_code,
                event_description, event_meta, processing_result, processing_status,
                error_message, duration_ms, created_at, updated_at
            )
            SELECT
                id,
                {account_id_expr} AS account_id,
                event_type,
                session_id,
                trigger_scene,
                result_code,
                event_description,
                event_meta,
                processing_result,
                processing_status,
                error_message,
                duration_ms,
                created_at,
                updated_at
            FROM risk_control_logs
            WHERE {account_id_expr} IS NOT NULL
            """,
        )
        self._execute_sql(cursor, "SELECT COUNT(*) FROM risk_control_logs_new")
        migrated_rows = int((cursor.fetchone() or [0])[0] or 0)

        self._execute_sql(cursor, "DROP TABLE risk_control_logs")
        self._execute_sql(cursor, "ALTER TABLE risk_control_logs_new RENAME TO risk_control_logs")
        logger.info(f"数据库迁移完成：risk_control_logs 已切换为 account_id 结构，迁移记录数: {migrated_rows}")

        self._execute_sql(cursor, "PRAGMA table_info(risk_control_logs)")
        return [column[1] for column in cursor.fetchall()]

    def _migrate_database(self, cursor):
        """执行数据库迁移"""
        try:
            # 检查cards表是否存在image_url列
            cursor.execute("PRAGMA table_info(cards)")
            columns = [column[1] for column in cursor.fetchall()]

            if 'image_url' not in columns:
                logger.info("添加cards表的image_url列...")
                cursor.execute("ALTER TABLE cards ADD COLUMN image_url TEXT")
                logger.info("数据库迁移完成：添加image_url列")

            # 检查并更新CHECK约束（重建表以支持image类型）
            self._update_cards_table_constraints(cursor)

            # 检查cookies表是否存在remark列
            cursor.execute("PRAGMA table_info(cookies)")
            cookie_columns = [column[1] for column in cursor.fetchall()]

            if 'remark' not in cookie_columns:
                logger.info("添加cookies表的remark列...")
                cursor.execute("ALTER TABLE cookies ADD COLUMN remark TEXT DEFAULT ''")
                logger.info("数据库迁移完成：添加remark列")

            # 检查cookies表是否存在pause_duration列
            if 'pause_duration' not in cookie_columns:
                logger.info("添加cookies表的pause_duration列...")
                cursor.execute("ALTER TABLE cookies ADD COLUMN pause_duration INTEGER DEFAULT 10")
                logger.info("数据库迁移完成：添加pause_duration列")

            # 检查cookies表是否存在auto_comment列
            if 'auto_comment' not in cookie_columns:
                logger.info("添加cookies表的auto_comment列...")
                cursor.execute("ALTER TABLE cookies ADD COLUMN auto_comment INTEGER DEFAULT 0")
                logger.info("数据库迁移完成：添加auto_comment列")

            self._ensure_cookie_binding_columns(cursor)

            # 历史版本可能缺少订单平台时间字段，不能再依赖旧版本号分支触发
            self._ensure_orders_platform_time_columns(cursor)

            # 迁移notification_templates表以支持新的模板类型
            self._migrate_notification_templates(cursor)

            # 检查ai_reply_settings表是否存在api_type列
            cursor.execute("PRAGMA table_info(ai_reply_settings)")
            ai_columns = [column[1] for column in cursor.fetchall()]
            if 'api_type' not in ai_columns:
                logger.info("添加ai_reply_settings表的api_type列...")
                cursor.execute("ALTER TABLE ai_reply_settings ADD COLUMN api_type TEXT DEFAULT ''")
                logger.info("数据库迁移完成：添加api_type列")

            # 检查ai_config_presets表是否存在api_type列
            cursor.execute("PRAGMA table_info(ai_config_presets)")
            preset_columns = [column[1] for column in cursor.fetchall()]
            if 'api_type' not in preset_columns:
                logger.info("添加ai_config_presets表的api_type列...")
                cursor.execute("ALTER TABLE ai_config_presets ADD COLUMN api_type TEXT NOT NULL DEFAULT ''")
                logger.info("数据库迁移完成：添加ai_config_presets.api_type列")

            # 检查risk_control_logs表扩展字段
            cursor.execute("PRAGMA table_info(risk_control_logs)")
            risk_log_columns = [column[1] for column in cursor.fetchall()]
            risk_log_column_defs = {
                'session_id': "TEXT",
                'trigger_scene': "TEXT",
                'result_code': "TEXT",
                'event_meta': "TEXT",
                'duration_ms': "INTEGER",
            }
            for column_name, column_type in risk_log_column_defs.items():
                if column_name not in risk_log_columns:
                    logger.info(f"添加risk_control_logs表的{column_name}列...")
                    cursor.execute(f"ALTER TABLE risk_control_logs ADD COLUMN {column_name} {column_type}")
                    logger.info(f"数据库迁移完成：添加risk_control_logs.{column_name}列")

            risk_log_columns = self._ensure_risk_control_logs_account_schema(cursor)

            self._execute_sql(cursor, "CREATE INDEX IF NOT EXISTS idx_risk_control_logs_account_created ON risk_control_logs(account_id, created_at DESC)")
            self._execute_sql(cursor, "CREATE INDEX IF NOT EXISTS idx_risk_control_logs_type_status_created ON risk_control_logs(event_type, processing_status, created_at DESC)")
            self._execute_sql(cursor, "CREATE INDEX IF NOT EXISTS idx_risk_control_logs_session_id ON risk_control_logs(session_id)")

        except Exception as e:
            logger.error(f"数据库迁移失败: {e}")
            # 迁移失败不应该阻止程序启动
            pass

    def _ensure_orders_platform_time_columns(self, cursor):
        """确保 orders 表存在平台时间字段。"""
        for order_time_column in ("platform_created_at", "platform_paid_at", "platform_completed_at"):
            try:
                self._execute_sql(cursor, f"SELECT {order_time_column} FROM orders LIMIT 1")
            except sqlite3.OperationalError:
                self._execute_sql(cursor, f"ALTER TABLE orders ADD COLUMN {order_time_column} TIMESTAMP")
                logger.info(f"为orders表添加平台时间字段({order_time_column})")

    def _update_cards_table_constraints(self, cursor):
        """更新cards表的CHECK约束以支持image和yifan_api类型"""
        try:
            # 尝试插入一个测试的yifan_api类型记录来检查约束
            cursor.execute('''
                INSERT INTO cards (name, type, user_id)
                VALUES ('__test_yifan_constraint__', 'yifan_api', 1)
            ''')
            # 如果插入成功，立即删除测试记录
            cursor.execute("DELETE FROM cards WHERE name = '__test_yifan_constraint__'")
            logger.info("cards表约束检查通过，支持yifan_api类型")
        except Exception as e:
            if "CHECK constraint failed" in str(e) or "constraint" in str(e).lower():
                logger.info("检测到旧的CHECK约束，开始更新cards表以支持yifan_api类型...")

                # 重建表以更新约束
                try:
                    # 1. 创建新表
                    cursor.execute('''
                    CREATE TABLE IF NOT EXISTS cards_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        type TEXT NOT NULL CHECK (type IN ('api', 'yifan_api', 'text', 'data', 'image')),
                        api_config TEXT,
                        text_content TEXT,
                        data_content TEXT,
                        image_url TEXT,
                        description TEXT,
                        enabled BOOLEAN DEFAULT TRUE,
                        delay_seconds INTEGER DEFAULT 0,
                        is_multi_spec BOOLEAN DEFAULT FALSE,
                        spec_name TEXT,
                        spec_value TEXT,
                        spec_name_2 TEXT,
                        spec_value_2 TEXT,
                        user_id INTEGER NOT NULL DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users (id)
                    )
                    ''')

                    # 2. 复制数据（双规格字段设为NULL，由后续迁移填充）
                    cursor.execute('''
                    INSERT INTO cards_new (id, name, type, api_config, text_content, data_content, image_url,
                                          description, enabled, delay_seconds, is_multi_spec, spec_name, spec_value,
                                          spec_name_2, spec_value_2, user_id, created_at, updated_at)
                    SELECT id, name, type, api_config, text_content, data_content, image_url,
                           description, enabled, delay_seconds, is_multi_spec, spec_name, spec_value,
                           NULL, NULL, user_id, created_at, updated_at
                    FROM cards
                    ''')

                    # 3. 删除旧表
                    cursor.execute("DROP TABLE cards")

                    # 4. 重命名新表
                    cursor.execute("ALTER TABLE cards_new RENAME TO cards")

                    logger.info("cards表约束更新完成，现在支持image类型")

                except Exception as rebuild_error:
                    logger.error(f"重建cards表失败: {rebuild_error}")
                    # 如果重建失败，尝试回滚
                    try:
                        cursor.execute("DROP TABLE IF EXISTS cards_new")
                    except:
                        pass
            else:
                logger.error(f"检查cards表约束时出现未知错误: {e}")

    def _migrate_notification_templates(self, cursor):
        """迁移notification_templates表以支持新的模板类型和用户隔离。"""
        try:
            self._execute_sql(
                cursor,
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='notification_templates'",
            )
            create_table_row = cursor.fetchone()
            create_table_sql = str((create_table_row or [""])[0] or "")
            normalized_create_table_sql = "".join(create_table_sql.lower().split())

            needs_rebuild = (
                "user_id" not in normalized_create_table_sql
                or "unique(user_id,type)" not in normalized_create_table_sql
                or "cookie_refresh_success" not in create_table_sql
            )

            self._execute_sql(cursor, "SELECT id FROM users ORDER BY id")
            all_user_ids = [
                int(row[0])
                for row in cursor.fetchall()
                if row and row[0] is not None
            ]
            admin_user_id = all_user_ids[0] if all_user_ids else 1

            if needs_rebuild:
                logger.info("检测到通知模板仍使用遗留全局结构，开始重建为 user_id 隔离结构")
                self._execute_sql(cursor, "DROP TABLE IF EXISTS notification_templates_new")
                self._execute_sql(
                    cursor,
                    '''
                    CREATE TABLE IF NOT EXISTS notification_templates_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        type TEXT NOT NULL CHECK (type IN ('message', 'token_refresh', 'delivery', 'slider_success', 'face_verify', 'password_login_success', 'cookie_refresh_success')),
                        template TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, type),
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                    ''',
                )

                self._execute_sql(cursor, "PRAGMA table_info(notification_templates)")
                existing_columns = [column[1] for column in cursor.fetchall()]
                migrated_rows = []

                if "user_id" in existing_columns:
                    self._execute_sql(
                        cursor,
                        '''
                        SELECT type, template, user_id, created_at, updated_at
                        FROM notification_templates
                        ''',
                    )
                    for template_type, template, row_user_id, created_at, updated_at in cursor.fetchall():
                        migrated_rows.append(
                            (
                                int(row_user_id or admin_user_id),
                                template_type,
                                template,
                                created_at,
                                updated_at,
                            )
                        )
                else:
                    self._execute_sql(
                        cursor,
                        '''
                        SELECT type, template, created_at, updated_at
                        FROM notification_templates
                        ''',
                    )
                    legacy_rows = list(cursor.fetchall())
                    for user_id in all_user_ids:
                        for template_type, template, created_at, updated_at in legacy_rows:
                            migrated_rows.append(
                                (
                                    user_id,
                                    template_type,
                                    template,
                                    created_at,
                                    updated_at,
                                )
                            )

                if migrated_rows:
                    cursor.executemany(
                        '''
                        INSERT OR IGNORE INTO notification_templates_new
                        (user_id, type, template, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        ''',
                        migrated_rows,
                    )

                self._execute_sql(cursor, "DROP TABLE notification_templates")
                self._execute_sql(cursor, "ALTER TABLE notification_templates_new RENAME TO notification_templates")

            self._seed_notification_template_defaults(cursor, all_user_ids)

            old_slider_success_template = '''✅ 滑块验证成功，cookies已自动更新到数据库

账号: {account_id}
时间: {time}'''
            new_slider_success_template = '''✅ 滑块验证成功，{status_text}

账号: {account_id}
时间: {time}'''
            self._execute_sql(
                cursor,
                '''
                UPDATE notification_templates
                SET template = ?, updated_at = CURRENT_TIMESTAMP
                WHERE type = 'slider_success' AND template = ?
                ''',
                (new_slider_success_template, old_slider_success_template)
            )

            logger.info("通知模板迁移完成，已切换为 user_id 隔离结构")
        except Exception as e:
            logger.warning(f"迁移notification_templates表时出错（可能表不存在）: {e}")
            try:
                cursor.execute("DROP TABLE IF EXISTS notification_templates_new")
            except:
                pass

    def check_and_upgrade_db(self, cursor):
        """检查数据库版本并执行必要的升级"""
        try:
            # 获取当前数据库版本
            current_version = self.get_system_setting("db_version") or "1.0"
            logger.info(f"当前数据库版本: {current_version}")

            if current_version == "1.0":
                logger.info("开始升级数据库到版本1.0...")
                self.update_admin_user_id(cursor)
                self.set_system_setting("db_version", "1.0", "数据库版本号")
                logger.info("数据库升级到版本1.0完成")
            
            # 如果版本低于需要升级的版本，执行升级
            if current_version < "1.1":
                logger.info("开始升级数据库到版本1.1...")
                self.upgrade_notification_channels_table(cursor)
                self.set_system_setting("db_version", "1.1", "数据库版本号")
                logger.info("数据库升级到版本1.1完成")

            # 升级到版本1.2 - 支持更多通知渠道类型
            if current_version < "1.2":
                logger.info("开始升级数据库到版本1.2...")
                self.upgrade_notification_channels_types(cursor)
                self.set_system_setting("db_version", "1.2", "数据库版本号")
                logger.info("数据库升级到版本1.2完成")

            # 升级到版本1.3 - 添加关键词类型和图片URL字段
            if current_version < "1.3":
                logger.info("开始升级数据库到版本1.3...")
                self.upgrade_keywords_table_for_image_support(cursor)
                self.set_system_setting("db_version", "1.3", "数据库版本号")
                logger.info("数据库升级到版本1.3完成")
            
            
            # 升级到版本1.4 - 添加关键词类型和图片URL字段
            if current_version < "1.4":
                logger.info("开始升级数据库到版本1.4...")
                self.upgrade_notification_channels_types(cursor)
                self.set_system_setting("db_version", "1.4", "数据库版本号")
                logger.info("数据库升级到版本1.4完成")

            # 升级到版本1.5 - 为cookies表添加账号登录字段
            if current_version < "1.5":
                logger.info("开始升级数据库到版本1.5...")
                self.upgrade_cookies_table_for_account_login(cursor)
                self.set_system_setting("db_version", "1.5", "数据库版本号")
                logger.info("数据库升级到版本1.5完成")

            # 升级到版本1.6 - 为cookies表添加代理配置字段
            if current_version < "1.6":
                logger.info("开始升级数据库到版本1.6...")
                self.upgrade_cookies_table_for_proxy(cursor)
                self.set_system_setting("db_version", "1.6", "数据库版本号")
                logger.info("数据库升级到版本1.6完成")

            # 升级到版本1.7 - 为users表添加is_admin字段
            if current_version < "1.7":
                logger.info("开始升级数据库到版本1.7...")
                self.upgrade_users_table_for_admin(cursor)
                self.set_system_setting("db_version", "1.7", "数据库版本号")
                logger.info("数据库升级到版本1.7完成")

            # 迁移遗留数据（在所有版本升级完成后执行）
            self.migrate_legacy_data(cursor)

        except Exception as e:
            logger.error(f"数据库版本检查或升级失败: {e}")
            raise
            
    def update_admin_user_id(self, cursor):
        """更新admin用户ID"""
        try:
            logger.info("开始更新admin用户ID...")
            # 创建默认admin用户（只在首次初始化时创建）
            cursor.execute('SELECT COUNT(*) FROM users WHERE username = ?', ('admin',))
            admin_exists = cursor.fetchone()[0] > 0

            if not admin_exists:
                # 首次创建admin用户，设置默认密码和管理员权限
                default_password_hash = hashlib.sha256("admin123".encode()).hexdigest()
                # 检查is_admin列是否存在
                try:
                    cursor.execute('SELECT is_admin FROM users LIMIT 1')
                    cursor.execute('''
                    INSERT INTO users (username, email, password_hash, is_admin) VALUES
                    ('admin', 'admin@localhost', ?, 1)
                    ''', (default_password_hash,))
                except sqlite3.OperationalError:
                    # is_admin列不存在，使用旧的INSERT语句
                    cursor.execute('''
                    INSERT INTO users (username, email, password_hash) VALUES
                    ('admin', 'admin@localhost', ?)
                    ''', (default_password_hash,))
                logger.info("创建默认admin用户，默认密码已初始化，请尽快修改")

            # 获取admin用户ID，用于历史数据绑定
            self._execute_sql(cursor, "SELECT id FROM users WHERE username = 'admin'")
            admin_user = cursor.fetchone()
            if admin_user:
                admin_user_id = admin_user[0]

                # 将历史cookies数据绑定到admin用户（如果user_id列不存在）
                try:
                    self._execute_sql(cursor, "SELECT user_id FROM cookies LIMIT 1")
                except sqlite3.OperationalError:
                    # user_id列不存在，需要添加并更新历史数据
                    self._execute_sql(cursor, "ALTER TABLE cookies ADD COLUMN user_id INTEGER")
                    self._execute_sql(cursor, "UPDATE cookies SET user_id = ? WHERE user_id IS NULL", (admin_user_id,))
                else:
                    # user_id列存在，更新NULL值
                    self._execute_sql(cursor, "UPDATE cookies SET user_id = ? WHERE user_id IS NULL", (admin_user_id,))

                # 为cookies表添加auto_confirm字段（如果不存在）
                try:
                    self._execute_sql(cursor, "SELECT auto_confirm FROM cookies LIMIT 1")
                except sqlite3.OperationalError:
                    # auto_confirm列不存在，需要添加并设置默认值
                    self._execute_sql(cursor, "ALTER TABLE cookies ADD COLUMN auto_confirm INTEGER DEFAULT 1")
                    self._execute_sql(cursor, "UPDATE cookies SET auto_confirm = 1 WHERE auto_confirm IS NULL")
                else:
                    # auto_confirm列存在，更新NULL值
                    self._execute_sql(cursor, "UPDATE cookies SET auto_confirm = 1 WHERE auto_confirm IS NULL")

                # 为delivery_rules表添加user_id字段（如果不存在）
                try:
                    self._execute_sql(cursor, "SELECT user_id FROM delivery_rules LIMIT 1")
                except sqlite3.OperationalError:
                    # user_id列不存在，需要添加并更新历史数据
                    self._execute_sql(cursor, "ALTER TABLE delivery_rules ADD COLUMN user_id INTEGER")
                    self._execute_sql(cursor, "UPDATE delivery_rules SET user_id = ? WHERE user_id IS NULL", (admin_user_id,))
                else:
                    # user_id列存在，更新NULL值
                    self._execute_sql(cursor, "UPDATE delivery_rules SET user_id = ? WHERE user_id IS NULL", (admin_user_id,))

                # 为delivery_rules表添加今日发货统计字段（如果不存在）
                try:
                    self._execute_sql(cursor, "SELECT last_delivery_date FROM delivery_rules LIMIT 1")
                except sqlite3.OperationalError:
                    # 今日发货字段不存在，需要添加
                    self._execute_sql(cursor, "ALTER TABLE delivery_rules ADD COLUMN last_delivery_date DATE")
                    self._execute_sql(cursor, "ALTER TABLE delivery_rules ADD COLUMN today_delivery_times INTEGER DEFAULT 0")
                    logger.info("已添加 last_delivery_date 和 today_delivery_times 字段到 delivery_rules 表")

                # 为notification_channels表添加user_id字段（如果不存在）
                try:
                    self._execute_sql(cursor, "SELECT user_id FROM notification_channels LIMIT 1")
                except sqlite3.OperationalError:
                    # user_id列不存在，需要添加并更新历史数据
                    self._execute_sql(cursor, "ALTER TABLE notification_channels ADD COLUMN user_id INTEGER")
                    self._execute_sql(cursor, "UPDATE notification_channels SET user_id = ? WHERE user_id IS NULL", (admin_user_id,))
                else:
                    # user_id列存在，更新NULL值
                    self._execute_sql(cursor, "UPDATE notification_channels SET user_id = ? WHERE user_id IS NULL", (admin_user_id,))

                # 为email_verifications表添加type字段（如果不存在）
                try:
                    self._execute_sql(cursor, "SELECT type FROM email_verifications LIMIT 1")
                except sqlite3.OperationalError:
                    # type列不存在，需要添加并更新历史数据
                    self._execute_sql(cursor, "ALTER TABLE email_verifications ADD COLUMN type TEXT DEFAULT 'register'")
                    self._execute_sql(cursor, "UPDATE email_verifications SET type = 'register' WHERE type IS NULL")
                else:
                    # type列存在，更新NULL值
                    self._execute_sql(cursor, "UPDATE email_verifications SET type = 'register' WHERE type IS NULL")

                # 为cards表添加多规格字段（如果不存在）
                try:
                    self._execute_sql(cursor, "SELECT is_multi_spec FROM cards LIMIT 1")
                except sqlite3.OperationalError:
                    # 多规格字段不存在，需要添加
                    self._execute_sql(cursor, "ALTER TABLE cards ADD COLUMN is_multi_spec BOOLEAN DEFAULT FALSE")
                    self._execute_sql(cursor, "ALTER TABLE cards ADD COLUMN spec_name TEXT")
                    self._execute_sql(cursor, "ALTER TABLE cards ADD COLUMN spec_value TEXT")
                    logger.info("为cards表添加多规格字段")

                # 为cards表添加双规格字段（如果不存在）
                try:
                    self._execute_sql(cursor, "SELECT spec_name_2 FROM cards LIMIT 1")
                except sqlite3.OperationalError:
                    # 双规格字段不存在，需要添加
                    self._execute_sql(cursor, "ALTER TABLE cards ADD COLUMN spec_name_2 TEXT")
                    self._execute_sql(cursor, "ALTER TABLE cards ADD COLUMN spec_value_2 TEXT")
                    logger.info("为cards表添加双规格字段(spec_name_2, spec_value_2)")

                # 为orders表添加双规格字段（如果不存在）
                try:
                    self._execute_sql(cursor, "SELECT spec_name_2 FROM orders LIMIT 1")
                except sqlite3.OperationalError:
                    # 双规格字段不存在，需要添加
                    self._execute_sql(cursor, "ALTER TABLE orders ADD COLUMN spec_name_2 TEXT")
                    self._execute_sql(cursor, "ALTER TABLE orders ADD COLUMN spec_value_2 TEXT")
                    logger.info("为orders表添加双规格字段(spec_name_2, spec_value_2)")

                self._ensure_orders_platform_time_columns(cursor)

                # 为item_info表添加多规格字段（如果不存在）
                try:
                    self._execute_sql(cursor, "SELECT is_multi_spec FROM item_info LIMIT 1")
                except sqlite3.OperationalError:
                    # 多规格字段不存在，需要添加
                    self._execute_sql(cursor, "ALTER TABLE item_info ADD COLUMN is_multi_spec BOOLEAN DEFAULT FALSE")
                    logger.info("为item_info表添加多规格字段")

                # 为item_info表添加多数量发货字段（如果不存在）
                try:
                    self._execute_sql(cursor, "SELECT multi_quantity_delivery FROM item_info LIMIT 1")
                except sqlite3.OperationalError:
                    # 多数量发货字段不存在，需要添加
                    self._execute_sql(cursor, "ALTER TABLE item_info ADD COLUMN multi_quantity_delivery BOOLEAN DEFAULT FALSE")
                    logger.info("为item_info表添加多数量发货字段")

                # 处理keywords表的唯一约束问题
                # 由于SQLite不支持直接修改约束，我们需要重建表
                self._migrate_keywords_table_constraints(cursor)

            self.conn.commit()
            logger.info(f"admin用户ID更新完成")
        except Exception as e:
            logger.error(f"更新admin用户ID失败: {e}")
            raise
            
    def upgrade_notification_channels_table(self, cursor):
        """升级notification_channels表的type字段约束"""
        try:
            logger.info("开始升级notification_channels表...")
            
            # 检查表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='notification_channels'")
            if not cursor.fetchone():
                logger.info("notification_channels表不存在，无需升级")
                return True
                
            # 检查表中是否有数据
            cursor.execute("SELECT COUNT(*) FROM notification_channels")
            count = cursor.fetchone()[0]

            # 删除可能存在的临时表
            cursor.execute("DROP TABLE IF EXISTS notification_channels_new")

            # 创建临时表
            cursor.execute('''
            CREATE TABLE notification_channels_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK (type IN ('qq','ding_talk')),
                config TEXT NOT NULL,
                enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            # 复制数据，并转换不兼容的类型
            if count > 0:
                logger.info(f"复制 {count} 条通知渠道数据到新表")
                # 先查看现有数据的类型
                cursor.execute("SELECT DISTINCT type FROM notification_channels")
                existing_types = [row[0] for row in cursor.fetchall()]
                logger.info(f"现有通知渠道类型: {existing_types}")

                # 获取所有现有数据进行逐行处理
                cursor.execute("SELECT * FROM notification_channels")
                existing_data = cursor.fetchall()

                # 逐行转移数据，确保类型映射正确
                for row in existing_data:
                    old_type = row[3] if len(row) > 3 else 'qq'  # type字段，默认为qq

                    # 类型映射规则
                    type_mapping = {
                        'dingtalk': 'ding_talk',
                        'ding_talk': 'ding_talk',
                        'qq': 'qq',
                        'email': 'qq',  # 暂时映射为qq，后续版本会支持
                        'webhook': 'qq',  # 暂时映射为qq，后续版本会支持
                        'wechat': 'qq',  # 暂时映射为qq，后续版本会支持
                        'telegram': 'qq'  # 暂时映射为qq，后续版本会支持
                    }

                    new_type = type_mapping.get(old_type, 'qq')  # 默认转换为qq类型

                    if old_type != new_type:
                        logger.info(f"转换通知渠道类型: {old_type} -> {new_type}")

                    # 插入到新表
                    cursor.execute('''
                    INSERT INTO notification_channels_new
                    (id, name, user_id, type, config, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        row[0],  # id
                        row[1],  # name
                        row[2],  # user_id
                        new_type,  # type (转换后的)
                        row[4] if len(row) > 4 else '{}',  # config
                        row[5] if len(row) > 5 else True,  # enabled
                        row[6] if len(row) > 6 else None,  # created_at
                        row[7] if len(row) > 7 else None   # updated_at
                    ))
            
            # 删除旧表
            cursor.execute("DROP TABLE notification_channels")
            
            # 重命名新表
            cursor.execute("ALTER TABLE notification_channels_new RENAME TO notification_channels")
            
            logger.info("notification_channels表升级完成")
            return True
        except Exception as e:
            logger.error(f"升级notification_channels表失败: {e}")
            raise

    def upgrade_notification_channels_types(self, cursor):
        """升级notification_channels表支持更多渠道类型"""
        try:
            logger.info("开始升级notification_channels表支持更多渠道类型...")

            # 检查表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='notification_channels'")
            if not cursor.fetchone():
                logger.info("notification_channels表不存在，无需升级")
                return True

            # 检查表中是否有数据
            cursor.execute("SELECT COUNT(*) FROM notification_channels")
            count = cursor.fetchone()[0]

            # 获取现有数据
            existing_data = []
            if count > 0:
                cursor.execute("SELECT * FROM notification_channels")
                existing_data = cursor.fetchall()
                logger.info(f"备份 {count} 条通知渠道数据")

            # 创建新表，支持所有通知渠道类型
            cursor.execute('''
            CREATE TABLE notification_channels_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK (type IN ('qq','ding_talk','dingtalk','feishu','lark','bark','email','webhook','wechat','telegram')),
                config TEXT NOT NULL,
                enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')

            # 复制数据，同时处理类型映射
            if existing_data:
                logger.info(f"迁移 {len(existing_data)} 条通知渠道数据到新表")
                for row in existing_data:
                    # 处理类型映射，支持更多渠道类型
                    old_type = row[3] if len(row) > 3 else 'qq'  # type字段

                    # 完整的类型映射规则，支持所有通知渠道
                    type_mapping = {
                        'ding_talk': 'dingtalk',  # 统一为dingtalk
                        'dingtalk': 'dingtalk',
                        'qq': 'qq',
                        'feishu': 'feishu',      # 飞书通知
                        'lark': 'lark',          # 飞书通知（英文名）
                        'bark': 'bark',          # Bark通知
                        'email': 'email',        # 邮件通知
                        'webhook': 'webhook',    # Webhook通知
                        'wechat': 'wechat',      # 微信通知
                        'telegram': 'telegram'   # Telegram通知
                    }

                    new_type = type_mapping.get(old_type, 'qq')  # 默认为qq

                    if old_type != new_type:
                        logger.info(f"转换通知渠道类型: {old_type} -> {new_type}")

                    # 插入到新表，确保字段完整性
                    cursor.execute('''
                    INSERT INTO notification_channels_new
                    (id, name, user_id, type, config, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        row[0],  # id
                        row[1],  # name
                        row[2],  # user_id
                        new_type,  # type (转换后的)
                        row[4] if len(row) > 4 else '{}',  # config
                        row[5] if len(row) > 5 else True,  # enabled
                        row[6] if len(row) > 6 else None,  # created_at
                        row[7] if len(row) > 7 else None   # updated_at
                    ))

            # 删除旧表
            cursor.execute("DROP TABLE notification_channels")

            # 重命名新表
            cursor.execute("ALTER TABLE notification_channels_new RENAME TO notification_channels")

            logger.info("notification_channels表类型升级完成")
            logger.info("✅ 现在支持以下所有通知渠道类型:")
            logger.info("   - qq (QQ通知)")
            logger.info("   - ding_talk/dingtalk (钉钉通知)")
            logger.info("   - feishu/lark (飞书通知)")
            logger.info("   - bark (Bark通知)")
            logger.info("   - email (邮件通知)")
            logger.info("   - webhook (Webhook通知)")
            logger.info("   - wechat (微信通知)")
            logger.info("   - telegram (Telegram通知)")
            return True
        except Exception as e:
            logger.error(f"升级notification_channels表类型失败: {e}")
            raise

    def upgrade_cookies_table_for_account_login(self, cursor):
        """升级cookies表支持账号密码登录功能"""
        try:
            logger.info("开始为cookies表添加账号登录相关字段...")

            # 为cookies表添加username字段（如果不存在）
            try:
                self._execute_sql(cursor, "SELECT username FROM cookies LIMIT 1")
                logger.info("cookies表username字段已存在")
            except sqlite3.OperationalError:
                # username字段不存在，需要添加
                self._execute_sql(cursor, "ALTER TABLE cookies ADD COLUMN username TEXT DEFAULT ''")
                logger.info("为cookies表添加username字段")

            # 为cookies表添加password字段（如果不存在）
            try:
                self._execute_sql(cursor, "SELECT password FROM cookies LIMIT 1")
                logger.info("cookies表password字段已存在")
            except sqlite3.OperationalError:
                # password字段不存在，需要添加
                self._execute_sql(cursor, "ALTER TABLE cookies ADD COLUMN password TEXT DEFAULT ''")
                logger.info("为cookies表添加password字段")

            # 为cookies表添加show_browser字段（如果不存在）
            try:
                self._execute_sql(cursor, "SELECT show_browser FROM cookies LIMIT 1")
                logger.info("cookies表show_browser字段已存在")
            except sqlite3.OperationalError:
                # show_browser字段不存在，需要添加
                self._execute_sql(cursor, "ALTER TABLE cookies ADD COLUMN show_browser INTEGER DEFAULT 0")
                logger.info("为cookies表添加show_browser字段")

            logger.info("✅ cookies表账号登录字段升级完成")
            logger.info("   - username: 用于密码登录的用户名")
            logger.info("   - password: 用于密码登录的密码")
            logger.info("   - show_browser: 登录时是否显示浏览器（0=隐藏，1=显示）")
            return True
        except Exception as e:
            logger.error(f"升级cookies表账号登录字段失败: {e}")
            raise

    def upgrade_cookies_table_for_proxy(self, cursor):
        """升级cookies表支持代理配置功能"""
        try:
            logger.info("开始为cookies表添加代理配置相关字段...")

            # 为cookies表添加proxy_type字段（代理类型：none/http/https/socks5）
            try:
                self._execute_sql(cursor, "SELECT proxy_type FROM cookies LIMIT 1")
                logger.info("cookies表proxy_type字段已存在")
            except sqlite3.OperationalError:
                self._execute_sql(cursor, "ALTER TABLE cookies ADD COLUMN proxy_type TEXT DEFAULT 'none'")
                logger.info("为cookies表添加proxy_type字段")

            # 为cookies表添加proxy_host字段（代理服务器地址）
            try:
                self._execute_sql(cursor, "SELECT proxy_host FROM cookies LIMIT 1")
                logger.info("cookies表proxy_host字段已存在")
            except sqlite3.OperationalError:
                self._execute_sql(cursor, "ALTER TABLE cookies ADD COLUMN proxy_host TEXT DEFAULT ''")
                logger.info("为cookies表添加proxy_host字段")

            # 为cookies表添加proxy_port字段（代理端口）
            try:
                self._execute_sql(cursor, "SELECT proxy_port FROM cookies LIMIT 1")
                logger.info("cookies表proxy_port字段已存在")
            except sqlite3.OperationalError:
                self._execute_sql(cursor, "ALTER TABLE cookies ADD COLUMN proxy_port INTEGER DEFAULT 0")
                logger.info("为cookies表添加proxy_port字段")

            # 为cookies表添加proxy_user字段（代理认证用户名）
            try:
                self._execute_sql(cursor, "SELECT proxy_user FROM cookies LIMIT 1")
                logger.info("cookies表proxy_user字段已存在")
            except sqlite3.OperationalError:
                self._execute_sql(cursor, "ALTER TABLE cookies ADD COLUMN proxy_user TEXT DEFAULT ''")
                logger.info("为cookies表添加proxy_user字段")

            # 为cookies表添加proxy_pass字段（代理认证密码）
            try:
                self._execute_sql(cursor, "SELECT proxy_pass FROM cookies LIMIT 1")
                logger.info("cookies表proxy_pass字段已存在")
            except sqlite3.OperationalError:
                self._execute_sql(cursor, "ALTER TABLE cookies ADD COLUMN proxy_pass TEXT DEFAULT ''")
                logger.info("为cookies表添加proxy_pass字段")

            logger.info("✅ cookies表代理配置字段升级完成")
            logger.info("   - proxy_type: 代理类型 (none/http/https/socks5)")
            logger.info("   - proxy_host: 代理服务器地址")
            logger.info("   - proxy_port: 代理端口")
            logger.info("   - proxy_user: 代理认证用户名（可选）")
            logger.info("   - proxy_pass: 代理认证密码（可选）")
            return True
        except Exception as e:
            logger.error(f"升级cookies表代理配置字段失败: {e}")
            raise

    def upgrade_users_table_for_admin(self, cursor):
        """升级users表支持管理员权限字段"""
        try:
            logger.info("开始为users表添加管理员权限字段...")

            # 为users表添加is_admin字段（如果不存在）
            try:
                self._execute_sql(cursor, "SELECT is_admin FROM users LIMIT 1")
                logger.info("users表is_admin字段已存在")
            except sqlite3.OperationalError:
                # is_admin字段不存在，需要添加
                self._execute_sql(cursor, "ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT FALSE")
                logger.info("为users表添加is_admin字段")

            # 将admin用户设置为管理员
            self._execute_sql(cursor, "UPDATE users SET is_admin = 1 WHERE username = 'admin'")
            logger.info("已将admin用户设置为管理员")

            logger.info("✅ users表管理员权限字段升级完成")
            logger.info("   - is_admin: 是否为管理员 (0=普通用户, 1=管理员)")
            return True
        except Exception as e:
            logger.error(f"升级users表管理员权限字段失败: {e}")
            raise

    def migrate_legacy_data(self, cursor):
        """迁移遗留数据到新表结构"""
        try:
            logger.info("开始检查和迁移遗留数据...")

            # 检查是否有需要迁移的老表
            legacy_tables = [
                'old_notification_channels',
                'legacy_delivery_rules',
                'old_keywords',
                'backup_cookies'
            ]

            for table_name in legacy_tables:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
                if cursor.fetchone():
                    logger.info(f"发现遗留表: {table_name}，开始迁移数据...")
                    self._migrate_table_data(cursor, table_name)

            logger.info("遗留数据迁移完成")
            return True
        except Exception as e:
            logger.error(f"迁移遗留数据失败: {e}")
            return False

    def _migrate_table_data(self, cursor, table_name: str):
        """迁移指定表的数据"""
        try:
            if table_name == 'old_notification_channels':
                # 迁移通知渠道数据
                cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
                count = cursor.fetchone()[0]

                if count > 0:
                    cursor.execute(f"SELECT * FROM {table_name}")
                    old_data = cursor.fetchall()

                    for row in old_data:
                        # 处理数据格式转换
                        cursor.execute('''
                        INSERT OR IGNORE INTO notification_channels
                        (name, user_id, type, config, enabled)
                        VALUES (?, ?, ?, ?, ?)
                        ''', (
                            row[1] if len(row) > 1 else f"迁移渠道_{row[0]}",
                            row[2] if len(row) > 2 else 1,  # 默认admin用户
                            self._normalize_channel_type(row[3] if len(row) > 3 else 'qq'),
                            row[4] if len(row) > 4 else '{}',
                            row[5] if len(row) > 5 else True
                        ))

                    logger.info(f"成功迁移 {count} 条通知渠道数据")

                    # 迁移完成后删除老表
                    cursor.execute(f"DROP TABLE {table_name}")
                    logger.info(f"已删除遗留表: {table_name}")

        except Exception as e:
            logger.error(f"迁移表 {table_name} 数据失败: {e}")

    def _normalize_channel_type(self, old_type: str) -> str:
        """标准化通知渠道类型"""
        type_mapping = {
            'ding_talk': 'dingtalk',
            'dingtalk': 'dingtalk',
            'qq': 'qq',
            'email': 'email',
            'webhook': 'webhook',
            'wechat': 'wechat',
            'telegram': 'telegram',
            # 处理一些可能的变体
            'dingding': 'dingtalk',
            'weixin': 'wechat',
            'tg': 'telegram'
        }
        return type_mapping.get(old_type.lower(), 'qq')
    
    def _migrate_keywords_table_constraints(self, cursor):
        """迁移keywords表的约束，支持基于商品ID的唯一性校验"""
        try:
            # 检查是否已经迁移过（通过检查是否存在新的唯一索引）
            cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_keywords_unique_with_item'")
            if cursor.fetchone():
                logger.info("keywords表约束已经迁移过，跳过")
                return

            logger.info("开始迁移keywords表约束...")

            # 1. 创建临时表，不设置主键约束
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS keywords_temp (
                account_id TEXT,
                keyword TEXT,
                reply TEXT,
                item_id TEXT,
                FOREIGN KEY (account_id) REFERENCES cookies(id) ON DELETE CASCADE
            )
            ''')

            # 2. 复制现有数据到临时表
            cursor.execute('''
            INSERT INTO keywords_temp (account_id, keyword, reply, item_id)
            SELECT account_id, keyword, reply, item_id FROM keywords
            ''')

            # 3. 删除原表
            cursor.execute('DROP TABLE keywords')

            # 4. 重命名临时表
            cursor.execute('ALTER TABLE keywords_temp RENAME TO keywords')

            # 5. 创建复合唯一索引来实现我们需要的约束逻辑
            # 对于item_id为空的情况：(account_id, keyword)必须唯一
            cursor.execute('''
            CREATE UNIQUE INDEX idx_keywords_unique_no_item
            ON keywords(account_id, keyword)
            WHERE item_id IS NULL OR item_id = ''
            ''')

            # 对于item_id不为空的情况：(account_id, keyword, item_id)必须唯一
            cursor.execute('''
            CREATE UNIQUE INDEX idx_keywords_unique_with_item
            ON keywords(account_id, keyword, item_id)
            WHERE item_id IS NOT NULL AND item_id != ''
            ''')

            logger.info("keywords表约束迁移完成")

        except Exception as e:
            logger.error(f"迁移keywords表约束失败: {e}")
            # 如果迁移失败，尝试回滚
            try:
                cursor.execute('DROP TABLE IF EXISTS keywords_temp')
            except:
                pass
            raise

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
            self.conn = None
    
    def get_connection(self):
        """获取数据库连接，如果已关闭则重新连接"""
        if self.conn is None:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._configure_connection()
        return self.conn

    def _log_sql(self, sql: str, params: tuple = None, operation: str = "EXECUTE"):
        """记录SQL执行日志"""
        if not self.sql_log_enabled:
            return

        # 格式化SQL（移除多余空白）
        formatted_sql = ' '.join(sql.split())
        sql_lower = formatted_sql.lower()
        sensitive_keywords = ('password', 'proxy_pass', 'smtp_password', 'admin_password_hash')
        contains_sensitive = any(keyword in sql_lower for keyword in sensitive_keywords)

        # 格式化参数
        params_str = ""
        if params:
            # 包含敏感字段的SQL统一脱敏参数，避免日志泄露密码等敏感信息
            if contains_sensitive:
                if isinstance(params, (list, tuple)):
                    params_str = f" | 参数: [***敏感参数已脱敏，共{len(params)}项***]"
                else:
                    params_str = " | 参数: [***敏感参数已脱敏***]"
            elif isinstance(params, (list, tuple)):
                if len(params) > 0:
                    # 限制参数长度，避免日志过长
                    formatted_params = []
                    for param in params:
                        if isinstance(param, str) and len(param) > 100:
                            formatted_params.append(f"{param[:100]}...")
                        else:
                            formatted_params.append(repr(param))
                    params_str = f" | 参数: [{', '.join(formatted_params)}]"
            else:
                params_str = f" | 参数: {repr(params)}"

        # 根据配置的日志级别输出
        log_message = f"🗄️ SQL {operation}: {formatted_sql}{params_str}"

        if self.sql_log_level == 'DEBUG':
            logger.debug(log_message)
        elif self.sql_log_level == 'INFO':
            logger.info(log_message)
        elif self.sql_log_level == 'WARNING':
            logger.warning(log_message)
        else:
            logger.debug(log_message)

    def _execute_sql(self, cursor, sql: str, params: tuple = None):
        """执行SQL并记录日志"""
        self._log_sql(sql, params, "EXECUTE")
        if params:
            return cursor.execute(sql, params)
        else:
            return cursor.execute(sql)

    def _executemany_sql(self, cursor, sql: str, params_list):
        """批量执行SQL并记录日志"""
        self._log_sql(sql, f"批量执行 {len(params_list)} 条记录", "EXECUTEMANY")
        return cursor.executemany(sql, params_list)
    
    def execute_query(self, sql: str, params: tuple = None):
        """执行查询并返回结果"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if params:
                    cursor.execute(sql, params)
                else:
                    cursor.execute(sql)
                return cursor.fetchall()
            except Exception as e:
                logger.error(f"执行查询失败: {e}")
                raise
    
    # -------------------- Cookie操作 --------------------

    
    
    
    def get_all_cookies(self, user_id: int = None) -> Dict[str, str]:
        """获取所有Cookie（支持用户隔离）"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    self._execute_sql(cursor, "SELECT id, value FROM cookies WHERE user_id = ?", (user_id,))
                else:
                    self._execute_sql(cursor, "SELECT id, value FROM cookies")
                return {row[0]: self._decrypt_secret(row[1]) for row in cursor.fetchall()}
            except Exception as e:
                logger.error(f"获取所有Cookie失败: {e}")
                raise

    def get_account_ids(self, user_id: int = None) -> List[str]:
        """仅获取账号ID列表，避免在只需要ID时解密整包 Cookie 值。"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    self._execute_sql(cursor, "SELECT id FROM cookies WHERE user_id = ? ORDER BY id", (user_id,))
                else:
                    self._execute_sql(cursor, "SELECT id FROM cookies ORDER BY id")
                return [str(row[0]) for row in cursor.fetchall() if row and row[0]]
            except Exception as e:
                logger.error(f"获取账号ID列表失败: {e}")
                raise

    def get_cookie_list_metadata(self, account_id: str) -> Optional[Dict[str, Any]]:
        """获取账号列表/摘要场景需要的轻量元数据，不解密敏感字段。"""
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT id, remark, pause_duration, username, password
                    FROM cookies
                    WHERE id = ?
                    """,
                    (normalized_account_id,),
                )
                result = cursor.fetchone()
                if not result:
                    return None

                return {
                    "id": result[0],
                    "account_id": result[0],
                    "remark": result[1] or "",
                    "pause_duration": result[2] if result[2] is not None else 10,
                    "username": result[3] or "",
                    "has_password": bool(result[4]),
                }
            except Exception as e:
                logger.error(f"获取账号列表元数据失败: account_id={account_id}, error={e}")
                raise





    def create_cookie_account_placeholder(self, account_id: str, user_id: int, *, bind_status: str = 'pending_bind') -> bool:
        """创建账号占位记录，锁定 account_id 与 user_id 归属。"""
        normalized_account_id = self._require_account_id(account_id)
        normalized_bind_status = str(bind_status or 'pending_bind').strip() or 'pending_bind'
        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(cursor, "SELECT id FROM cookies WHERE id = ?", (normalized_account_id,))
                if cursor.fetchone():
                    logger.warning(f"账号占位记录已存在，拒绝重复创建: {normalized_account_id}")
                    return False

                self._execute_sql(
                    cursor,
                    """
                    INSERT INTO cookies (id, value, user_id, bind_status)
                    VALUES (?, ?, ?, ?)
                    """,
                    (normalized_account_id, '', user_id, normalized_bind_status),
                )
                self.conn.commit()
                logger.info(
                    f"创建账号占位记录成功: {normalized_account_id}, user_id={user_id}, bind_status={normalized_bind_status}"
                )
                return True
            except Exception as e:
                logger.error(f"创建账号占位记录失败: {e}")
                self.conn.rollback()
                return False

    def delete_pending_cookie_placeholder(self, account_id: str, user_id: int = None) -> bool:
        """仅删除仍处于 pending_bind 且尚未写入 Cookie 的账号占位记录。"""
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(
                    cursor,
                    """
                    SELECT user_id, bind_status, value
                    FROM cookies
                    WHERE id = ?
                    """,
                    (normalized_account_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return False

                existing_user_id, bind_status, encrypted_cookie_value = row
                if user_id is not None and existing_user_id != user_id:
                    logger.warning(
                        f"账号 {normalized_account_id} 占位归属用户 {existing_user_id}，拒绝按用户 {user_id} 删除"
                    )
                    return False

                if str(bind_status or '').strip() != 'pending_bind':
                    return False

                cookie_value = self._decrypt_secret(encrypted_cookie_value)
                if str(cookie_value or '').strip():
                    return False

                self._execute_sql(cursor, "DELETE FROM cookies WHERE id = ?", (normalized_account_id,))
                self.conn.commit()
                logger.info(f"已删除待绑定账号占位记录: {normalized_account_id}")
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"删除待绑定账号占位记录失败: account_id={account_id}, error={e}")
                self.conn.rollback()
                return False

    def get_cookie_binding_info(self, account_id: str) -> Optional[Dict[str, Any]]:
        """获取账号绑定信息。"""
        normalized_account_id = self._require_account_id(account_id)
        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(
                    cursor,
                    """
                    SELECT id, user_id, bound_unb, bind_status
                    FROM cookies
                    WHERE id = ?
                    """,
                    (normalized_account_id,),
                )
                result = cursor.fetchone()
                if not result:
                    return None
                return {
                    'account_id': result[0],
                    'user_id': result[1],
                    'bound_unb': result[2] or '',
                    'bind_status': result[3] or 'active',
                }
            except Exception as e:
                logger.error(f"获取账号绑定信息失败: {e}")
                raise

    def bind_cookie_account_unb(self, account_id: str, bound_unb: str, *, user_id: int = None) -> bool:
        """首次绑定账号 unb；已绑定到其他 unb 时拒绝覆盖。"""
        normalized_account_id = self._require_account_id(account_id)
        normalized_unb = str(bound_unb or '').strip()
        if not normalized_unb:
            logger.warning(f"账号 {normalized_account_id} 未提供有效 unb，拒绝绑定")
            return False

        with self.lock:
            try:
                if user_id is not None:
                    self.assert_cookie_belongs_to_user(normalized_account_id, user_id)

                binding_info = self.get_cookie_binding_info(normalized_account_id)
                if not binding_info:
                    logger.warning(f"账号不存在，无法绑定 unb: {normalized_account_id}")
                    return False

                existing_unb = str(binding_info.get('bound_unb') or '').strip()
                if existing_unb and existing_unb != normalized_unb:
                    logger.warning(
                        f"账号 {normalized_account_id} 已绑定其他 unb，拒绝覆盖: existing={existing_unb}, incoming={normalized_unb}"
                    )
                    return False

                cursor = self.conn.cursor()
                self._execute_sql(
                    cursor,
                    """
                    UPDATE cookies
                    SET bound_unb = ?, bind_status = 'active'
                    WHERE id = ?
                    """,
                    (normalized_unb, normalized_account_id),
                )
                if cursor.rowcount <= 0:
                    logger.warning(f"账号不存在，无法绑定 unb: {normalized_account_id}")
                    self.conn.rollback()
                    return False
                self.conn.commit()
                logger.info(f"账号 {normalized_account_id} 绑定 unb 成功: {normalized_unb}")
                return True
            except (PermissionError, KeyError):
                raise
            except Exception as e:
                logger.error(f"绑定账号 unb 失败: {e}")
                self.conn.rollback()
                return False

    def update_cookie_bind_status(self, account_id: str, bind_status: str) -> bool:
        """更新账号绑定状态。"""
        normalized_account_id = self._require_account_id(account_id)
        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(
                    cursor,
                    "UPDATE cookies SET bind_status = ? WHERE id = ?",
                    (bind_status, normalized_account_id),
                )
                if cursor.rowcount <= 0:
                    logger.warning(f"账号不存在，无法更新绑定状态: {normalized_account_id}")
                    self.conn.rollback()
                    return False
                self.conn.commit()
                logger.info(f"账号 {normalized_account_id} 绑定状态更新成功: {bind_status}")
                return True
            except Exception as e:
                logger.error(f"更新账号绑定状态失败: {e}")
                self.conn.rollback()
                return False

    def restore_cookie_binding_state(self, account_id: str, bound_unb: str, bind_status: str) -> bool:
        """按快照恢复账号绑定状态。"""
        normalized_account_id = self._require_account_id(account_id)
        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(
                    cursor,
                    """
                    UPDATE cookies
                    SET bound_unb = ?, bind_status = ?
                    WHERE id = ?
                    """,
                    ((bound_unb or ''), (bind_status or 'active'), normalized_account_id),
                )
                if cursor.rowcount <= 0:
                    logger.warning(f"账号不存在，无法恢复绑定状态: {normalized_account_id}")
                    self.conn.rollback()
                    return False
                self.conn.commit()
                logger.info(
                    f"账号 {normalized_account_id} 绑定状态已恢复: bound_unb={(bound_unb or '')}, bind_status={(bind_status or 'active')}"
                )
                return True
            except Exception as e:
                logger.error(f"恢复账号绑定状态失败: {e}")
                self.conn.rollback()
                return False

    def assert_cookie_belongs_to_user(self, account_id: str, user_id: int) -> bool:
        """校验账号是否归属于指定用户。"""
        normalized_account_id = self._require_account_id(account_id)
        binding_info = self.get_cookie_binding_info(normalized_account_id)
        if not binding_info:
            raise KeyError(f"账号不存在: {normalized_account_id}")

        if binding_info.get('user_id') != user_id:
            raise PermissionError(f"账号 {normalized_account_id} 不属于当前用户")

        return True









    # -------------------- 自动好评操作 --------------------







    
    # -------------------- 关键字操作 --------------------


    








    def get_all_keywords(self, user_id: int = None) -> Dict[str, List[Tuple[str, str]]]:
        """获取所有Cookie的关键字（支持用户隔离）"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    cursor.execute("""
                    SELECT k.account_id, k.keyword, k.reply
                    FROM keywords k
                    JOIN cookies c ON k.account_id = c.id
                    WHERE c.user_id = ?
                    """, (user_id,))
                else:
                    self._execute_sql(cursor, "SELECT account_id, keyword, reply FROM keywords")

                result = {}
                for row in cursor.fetchall():
                    account_id, keyword, reply = row
                    if account_id not in result:
                        result[account_id] = []
                    result[account_id].append((keyword, reply))

                return result
            except Exception as e:
                logger.error(f"获取所有关键字失败: {e}")
                raise




    # -------------------- AI回复设置操作 --------------------



    # -------------------- AI配置预设操作 --------------------
    def save_ai_config_preset(self, user_id: int, preset_name: str, model_name: str, api_key: str = '', base_url: str = '', api_type: str = '') -> int:
        """保存AI配置预设（存在则更新）"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute('''
                INSERT INTO ai_config_presets (user_id, preset_name, model_name, api_key, base_url, api_type, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, preset_name) DO UPDATE SET
                    model_name = excluded.model_name,
                    api_key = excluded.api_key,
                    base_url = excluded.base_url,
                    api_type = excluded.api_type,
                    updated_at = CURRENT_TIMESTAMP
                ''', (user_id, preset_name, model_name, api_key, base_url, api_type))
                self.conn.commit()
                preset_id = cursor.lastrowid
                logger.debug(f"保存AI配置预设: user_id={user_id}, preset_name={preset_name}")
                return preset_id
            except Exception as e:
                logger.error(f"保存AI配置预设失败: {e}")
                raise

    def get_ai_config_presets(self, user_id: int) -> list:
        """获取用户的所有AI配置预设"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute('''
                SELECT id, preset_name, model_name, api_key, base_url, api_type, created_at, updated_at
                FROM ai_config_presets
                WHERE user_id = ?
                ORDER BY updated_at DESC
                ''', (user_id,))
                presets = []
                for row in cursor.fetchall():
                    presets.append({
                        'id': row[0],
                        'preset_name': row[1],
                        'model_name': row[2],
                        'api_key': row[3],
                        'base_url': row[4],
                        'api_type': row[5] or '',
                        'created_at': row[6],
                        'updated_at': row[7]
                    })
                return presets
            except Exception as e:
                logger.error(f"获取AI配置预设失败: {e}")
                raise

    def delete_ai_config_preset(self, user_id: int, preset_id: int) -> bool:
        """删除AI配置预设（带user_id校验）"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute('''
                DELETE FROM ai_config_presets WHERE id = ? AND user_id = ?
                ''', (preset_id, user_id))
                self.conn.commit()
                deleted = cursor.rowcount > 0
                if deleted:
                    logger.debug(f"删除AI配置预设: preset_id={preset_id}, user_id={user_id}")
                return deleted
            except Exception as e:
                logger.error(f"删除AI配置预设失败: {e}")
                self.conn.rollback()
                raise

    # -------------------- 默认回复操作 --------------------







    # -------------------- 通知渠道操作 --------------------
    def _normalize_notification_channel_name(self, name: Any) -> str:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("通知渠道名称不能为空")
        return normalized_name

    def _normalize_notification_channel_type(self, channel_type: Any) -> str:
        normalized_type = str(channel_type or "").strip().lower()
        type_aliases = {
            'ding_talk': 'dingtalk',
            'dingtalk': 'dingtalk',
            'dingding': 'dingtalk',
            'lark': 'feishu',
            'feishu': 'feishu',
            'qq': 'qq',
            'bark': 'bark',
            'email': 'email',
            'webhook': 'webhook',
            'wechat': 'wechat',
            'weixin': 'wechat',
            'telegram': 'telegram',
            'tg': 'telegram',
        }
        resolved_type = type_aliases.get(normalized_type)
        if not resolved_type:
            raise ValueError("通知渠道类型无效")
        return resolved_type

    def create_notification_channel(
        self,
        name: str,
        channel_type: str,
        config: str,
        user_id: int = None,
        enabled: bool = True,
    ) -> int:
        """创建通知渠道"""
        with self.lock:
            try:
                normalized_name = self._normalize_notification_channel_name(name)
                normalized_type = self._normalize_notification_channel_type(channel_type)
                cursor = self.conn.cursor()
                cursor.execute('''
                INSERT INTO notification_channels (name, type, config, user_id, enabled)
                VALUES (?, ?, ?, ?, ?)
                ''', (normalized_name, normalized_type, config, user_id, int(enabled)))
                self.conn.commit()
                channel_id = cursor.lastrowid
                logger.debug(f"创建通知渠道: {normalized_name} (ID: {channel_id})")
                return channel_id
            except Exception as e:
                logger.error(f"创建通知渠道失败: {e}")
                self.conn.rollback()
                raise

    def get_notification_channels(self, user_id: int = None) -> List[Dict[str, any]]:
        """获取所有通知渠道"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    cursor.execute('''
                    SELECT id, name, type, config, enabled, created_at, updated_at
                    FROM notification_channels
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                    ''', (user_id,))
                else:
                    cursor.execute('''
                    SELECT id, name, type, config, enabled, created_at, updated_at
                    FROM notification_channels
                    ORDER BY created_at DESC
                    ''')

                channels = []
                for row in cursor.fetchall():
                    channels.append({
                        'id': row[0],
                        'name': row[1],
                        'type': row[2],
                        'config': row[3],
                        'enabled': bool(row[4]),
                        'created_at': row[5],
                        'updated_at': row[6]
                    })

                return channels
            except Exception as e:
                logger.error(f"获取通知渠道失败: {e}")
                raise

    def get_notification_channel(self, channel_id: int, user_id: int = None) -> Optional[Dict[str, any]]:
        """获取指定通知渠道"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    cursor.execute('''
                    SELECT id, name, type, config, enabled, created_at, updated_at, user_id
                    FROM notification_channels WHERE id = ? AND user_id = ?
                    ''', (channel_id, user_id))
                else:
                    cursor.execute('''
                    SELECT id, name, type, config, enabled, created_at, updated_at, user_id
                    FROM notification_channels WHERE id = ?
                    ''', (channel_id,))

                row = cursor.fetchone()
                if row:
                    return {
                        'id': row[0],
                        'name': row[1],
                        'type': row[2],
                        'config': row[3],
                        'enabled': bool(row[4]),
                        'created_at': row[5],
                        'updated_at': row[6],
                        'user_id': row[7]
                    }
                return None
            except Exception as e:
                logger.error(f"获取通知渠道失败: {e}")
                raise

    def update_notification_channel(self, channel_id: int, name: str, config: str, enabled: bool = True, user_id: int = None) -> bool:
        """更新通知渠道"""
        with self.lock:
            try:
                normalized_name = self._normalize_notification_channel_name(name)
                cursor = self.conn.cursor()
                if user_id is not None:
                    cursor.execute('''
                    UPDATE notification_channels
                    SET name = ?, config = ?, enabled = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND user_id = ?
                    ''', (normalized_name, config, enabled, channel_id, user_id))
                else:
                    cursor.execute('''
                    UPDATE notification_channels
                    SET name = ?, config = ?, enabled = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    ''', (normalized_name, config, enabled, channel_id))
                self.conn.commit()
                logger.debug(f"更新通知渠道: {channel_id}")
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"更新通知渠道失败: {e}")
                self.conn.rollback()
                raise

    def delete_notification_channel(self, channel_id: int, user_id: int = None) -> bool:
        """删除通知渠道"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    self._execute_sql(
                        cursor,
                        """
                        DELETE FROM message_notifications
                        WHERE channel_id IN (
                            SELECT id FROM notification_channels WHERE id = ? AND user_id = ?
                        )
                        """,
                        (channel_id, user_id),
                    )
                    self._execute_sql(
                        cursor,
                        "DELETE FROM notification_channels WHERE id = ? AND user_id = ?",
                        (channel_id, user_id),
                    )
                else:
                    self._execute_sql(
                        cursor,
                        "DELETE FROM message_notifications WHERE channel_id = ?",
                        (channel_id,),
                    )
                    self._execute_sql(cursor, "DELETE FROM notification_channels WHERE id = ?", (channel_id,))
                self.conn.commit()
                logger.debug(f"删除通知渠道: {channel_id}")
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"删除通知渠道失败: {e}")
                self.conn.rollback()
                raise

    # -------------------- 消息通知配置操作 --------------------



    def delete_message_notification(self, notification_id: int, user_id: int = None) -> bool:
        """删除消息通知配置"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    self._execute_sql(cursor, '''
                    DELETE FROM message_notifications
                    WHERE id = ? AND channel_id IN (
                        SELECT id FROM notification_channels WHERE user_id = ?
                    )
                    ''', (notification_id, user_id))
                else:
                    self._execute_sql(cursor, "DELETE FROM message_notifications WHERE id = ?", (notification_id,))
                self.conn.commit()
                logger.debug(f"删除消息通知配置: {notification_id}")
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"删除消息通知配置失败: {e}")
                self.conn.rollback()
                raise


    # -------------------- 通知模板操作 --------------------
    def _get_notification_template_defaults(self) -> Dict[str, str]:
        return {
            'message': '''🚨 接收消息通知

账号: {account_id}
买家: {buyer_name} (ID: {buyer_id})
商品ID: {item_id}
聊天ID: {chat_id}
消息内容: {message}

时间: {time}''',
            'token_refresh': '''Token刷新异常

账号ID: {account_id}
异常时间: {time}
异常信息: {error_message}

请检查账号Cookie是否过期，如有需要请及时更新Cookie配置。''',
            'delivery': '''🚨 自动发货通知

账号: {account_id}
买家: {buyer_name} (ID: {buyer_id})
商品ID: {item_id}
聊天ID: {chat_id}
结果: {result}
时间: {time}

请及时处理！''',
            'slider_success': '''✅ 滑块验证成功，{status_text}

账号: {account_id}
时间: {time}''',
            'face_verify': '''⚠️ 需要{verification_type} 🚫
在验证期间，发货及自动回复暂时无法使用。

{verification_action}
{verification_url}

账号: {account_id}
时间: {time}''',
            'password_login_success': '''✅ 密码登录成功

账号: {account_id}
时间: {time}
Cookie数量: {cookie_count}

账号Cookie已更新，正在重启服务...''',
            'cookie_refresh_success': '''✅ 刷新Cookie成功

账号: {account_id}
时间: {time}
Cookie数量: {cookie_count}

账号已可正常使用。''',
        }

    def _seed_notification_template_defaults(self, cursor, user_ids: List[int]) -> None:
        normalized_user_ids = []
        for user_id in user_ids or []:
            try:
                normalized_user_id = int(user_id)
            except (TypeError, ValueError):
                continue
            if normalized_user_id not in normalized_user_ids:
                normalized_user_ids.append(normalized_user_id)

        if not normalized_user_ids:
            return

        default_templates = self._get_notification_template_defaults()
        rows = []
        for user_id in normalized_user_ids:
            for template_type, template in default_templates.items():
                rows.append((user_id, template_type, template))

        cursor.executemany(
            '''
            INSERT OR IGNORE INTO notification_templates (user_id, type, template)
            VALUES (?, ?, ?)
            ''',
            rows,
        )

    def get_all_notification_templates(self, user_id: int = None) -> List[Dict[str, any]]:
        """获取通知模板列表"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    cursor.execute('''
                    SELECT id, type, template, created_at, updated_at, user_id
                    FROM notification_templates
                    WHERE user_id = ?
                    ORDER BY id
                    ''', (user_id,))
                else:
                    cursor.execute('''
                    SELECT id, type, template, created_at, updated_at, user_id
                    FROM notification_templates
                    ORDER BY user_id, id
                    ''')

                templates = []
                for row in cursor.fetchall():
                    templates.append({
                        'id': row[0],
                        'type': row[1],
                        'template': row[2],
                        'created_at': row[3],
                        'updated_at': row[4],
                        'user_id': row[5],
                    })

                return templates
            except Exception as e:
                logger.error(f"获取通知模板失败: {e}")
                raise

    def get_notification_template(self, template_type: str, user_id: int = None) -> Optional[Dict[str, any]]:
        """获取指定用户的通知模板"""
        with self.lock:
            try:
                if user_id is None:
                    logger.warning(f"获取通知模板缺少 user_id，拒绝跨用户读取: {template_type}")
                    return None
                cursor = self.conn.cursor()
                cursor.execute('''
                SELECT id, type, template, created_at, updated_at, user_id
                FROM notification_templates
                WHERE type = ? AND user_id = ?
                ''', (template_type, user_id))

                row = cursor.fetchone()
                if row:
                    return {
                        'id': row[0],
                        'type': row[1],
                        'template': row[2],
                        'created_at': row[3],
                        'updated_at': row[4],
                        'user_id': row[5],
                    }
                return None
            except Exception as e:
                logger.error(f"获取通知模板失败: {e}")
                raise

    def update_notification_template(self, template_type: str, template: str, user_id: int = None) -> bool:
        """更新指定用户的通知模板"""
        with self.lock:
            try:
                normalized_template = str(template or "").strip()
                if not normalized_template:
                    raise ValueError("通知模板内容不能为空")
                if user_id is None:
                    logger.warning(f"更新通知模板缺少 user_id，拒绝跨用户写入: {template_type}")
                    return False
                cursor = self.conn.cursor()
                self._seed_notification_template_defaults(cursor, [user_id])
                self._execute_sql(cursor, '''
                UPDATE notification_templates
                SET template = ?, updated_at = CURRENT_TIMESTAMP
                WHERE type = ? AND user_id = ?
                ''', (normalized_template, template_type, user_id))
                self.conn.commit()
                logger.info(f"更新通知模板: user_id={user_id}, type={template_type}")
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"更新通知模板失败: {e}")
                self.conn.rollback()
                raise

    def reset_notification_template(self, template_type: str, user_id: int = None) -> bool:
        """将指定用户的通知模板重置为默认值"""
        default_templates = self._get_notification_template_defaults()
        if template_type not in default_templates:
            logger.error(f"未知的模板类型: {template_type}")
            return False

        return self.update_notification_template(
            template_type,
            default_templates[template_type],
            user_id=user_id,
        )

    def get_default_notification_template(self, template_type: str) -> Optional[str]:
        """获取默认通知模板"""
        default_templates = self._get_notification_template_defaults()

        return default_templates.get(template_type)

    # -------------------- 备份和恢复操作 --------------------


    # -------------------- 系统设置操作 --------------------
    def get_system_setting(self, key: str) -> Optional[str]:
        """获取系统设置"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(cursor, "SELECT value FROM system_settings WHERE key = ?", (key,))
                result = cursor.fetchone()
                return result[0] if result else None
            except Exception as e:
                logger.error(f"获取系统设置失败: {e}")
                raise

    def set_system_setting(self, key: str, value: str, description: str = None) -> bool:
        """设置系统设置"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute('''
                INSERT OR REPLACE INTO system_settings (key, value, description, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ''', (key, value, description))
                self.conn.commit()
                logger.debug(f"设置系统设置: {key}")
                return True
            except Exception as e:
                logger.error(f"设置系统设置失败: {e}")
                self.conn.rollback()
                raise

    def get_all_system_settings(self) -> Dict[str, str]:
        """获取所有系统设置"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(cursor, "SELECT key, value FROM system_settings")

                settings = {}
                for row in cursor.fetchall():
                    settings[row[0]] = row[1]

                return settings
            except Exception as e:
                logger.error(f"获取所有系统设置失败: {e}")
                raise

    # 管理员密码现在统一使用用户表管理，不再需要单独的方法

    # ==================== 用户管理方法 ====================

    def create_user(self, username: str, email: str, password: str) -> bool:
        """创建新用户"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                password_hash = hashlib.sha256(password.encode()).hexdigest()

                cursor.execute('''
                INSERT INTO users (username, email, password_hash)
                VALUES (?, ?, ?)
                ''', (username, email, password_hash))
                self._seed_notification_template_defaults(cursor, [cursor.lastrowid])

                self.conn.commit()
                logger.info(f"创建用户成功: {username} ({email})")
                return True
            except sqlite3.IntegrityError as e:
                logger.error(f"创建用户失败，用户名或邮箱已存在: {e}")
                self.conn.rollback()
                return False
            except Exception as e:
                logger.error(f"创建用户失败: {e}")
                self.conn.rollback()
                return False

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """根据用户名获取用户信息"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                # 检查is_admin列是否存在
                cursor.execute("PRAGMA table_info(users)")
                columns = [col[1] for col in cursor.fetchall()]
                has_is_admin = 'is_admin' in columns

                if has_is_admin:
                    cursor.execute('''
                    SELECT id, username, email, password_hash, is_active, created_at, updated_at, is_admin
                    FROM users WHERE username = ?
                    ''', (username,))
                else:
                    cursor.execute('''
                    SELECT id, username, email, password_hash, is_active, created_at, updated_at
                    FROM users WHERE username = ?
                    ''', (username,))

                row = cursor.fetchone()
                if row:
                    user_data = {
                        'id': row[0],
                        'username': row[1],
                        'email': row[2],
                        'password_hash': row[3],
                        'is_active': row[4],
                        'created_at': row[5],
                        'updated_at': row[6],
                    }
                    if has_is_admin:
                        user_data['is_admin'] = bool(row[7]) if row[7] is not None else (row[1] == 'admin')
                    else:
                        user_data['is_admin'] = (row[1] == 'admin')
                    return user_data
                return None
            except Exception as e:
                logger.error(f"获取用户信息失败: {e}")
                return None

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """根据邮箱获取用户信息"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                # 检查is_admin列是否存在
                cursor.execute("PRAGMA table_info(users)")
                columns = [col[1] for col in cursor.fetchall()]
                has_is_admin = 'is_admin' in columns

                if has_is_admin:
                    cursor.execute('''
                    SELECT id, username, email, password_hash, is_active, created_at, updated_at, is_admin
                    FROM users WHERE email = ?
                    ''', (email,))
                else:
                    cursor.execute('''
                    SELECT id, username, email, password_hash, is_active, created_at, updated_at
                    FROM users WHERE email = ?
                    ''', (email,))

                row = cursor.fetchone()
                if row:
                    user_data = {
                        'id': row[0],
                        'username': row[1],
                        'email': row[2],
                        'password_hash': row[3],
                        'is_active': row[4],
                        'created_at': row[5],
                        'updated_at': row[6],
                    }
                    if has_is_admin:
                        user_data['is_admin'] = bool(row[7]) if row[7] is not None else (row[1] == 'admin')
                    else:
                        user_data['is_admin'] = (row[1] == 'admin')
                    return user_data
                return None
            except Exception as e:
                logger.error(f"获取用户信息失败: {e}")
                return None

    def verify_user_password(self, username: str, password: str) -> bool:
        """验证用户密码"""
        user = self.get_user_by_username(username)
        if not user:
            return False

        password_hash = hashlib.sha256(password.encode()).hexdigest()
        return user['password_hash'] == password_hash and user['is_active']

    def update_user_password(self, username: str, new_password: str) -> bool:
        """更新用户密码"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                password_hash = hashlib.sha256(new_password.encode()).hexdigest()

                cursor.execute('''
                UPDATE users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP
                WHERE username = ?
                ''', (password_hash, username))

                if cursor.rowcount > 0:
                    self.conn.commit()
                    logger.info(f"用户 {username} 密码更新成功")
                    return True
                else:
                    logger.warning(f"用户 {username} 不存在，密码更新失败")
                    return False

            except Exception as e:
                logger.error(f"更新用户密码失败: {e}")
                self.conn.rollback()
                return False

    def generate_verification_code(self) -> str:
        """生成6位数字验证码"""
        return ''.join(random.choices(string.digits, k=6))

    def generate_captcha(self) -> Tuple[str, str]:
        """生成图形验证码
        返回: (验证码文本, base64编码的图片)
        """
        try:
            # 生成4位随机验证码（数字+字母）
            chars = string.ascii_uppercase + string.digits
            captcha_text = ''.join(random.choices(chars, k=4))

            # 创建图片
            width, height = 120, 40
            image = Image.new('RGB', (width, height), color='white')
            draw = ImageDraw.Draw(image)

            # 尝试使用系统字体，如果失败则使用默认字体
            try:
                # Windows系统字体
                font = ImageFont.truetype("arial.ttf", 20)
            except:
                try:
                    # 备用字体
                    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 20)
                except:
                    # 使用默认字体
                    font = ImageFont.load_default()

            # 绘制验证码文本
            for i, char in enumerate(captcha_text):
                # 随机颜色
                color = (
                    random.randint(0, 100),
                    random.randint(0, 100),
                    random.randint(0, 100)
                )

                # 随机位置（稍微偏移）
                x = 20 + i * 20 + random.randint(-3, 3)
                y = 8 + random.randint(-3, 3)

                draw.text((x, y), char, font=font, fill=color)

            # 添加干扰线
            for _ in range(3):
                start = (random.randint(0, width), random.randint(0, height))
                end = (random.randint(0, width), random.randint(0, height))
                draw.line([start, end], fill=(random.randint(100, 200), random.randint(100, 200), random.randint(100, 200)), width=1)

            # 添加干扰点
            for _ in range(20):
                x = random.randint(0, width)
                y = random.randint(0, height)
                draw.point((x, y), fill=(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)))

            # 转换为base64
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            img_base64 = base64.b64encode(buffer.getvalue()).decode()

            return captcha_text, f"data:image/png;base64,{img_base64}"

        except Exception as e:
            logger.error(f"生成图形验证码失败: {e}")
            # 返回简单的文本验证码作为备用
            simple_code = ''.join(random.choices(string.digits, k=4))
            return simple_code, ""

    def save_captcha(self, session_id: str, captcha_text: str, expires_minutes: int = 5) -> bool:
        """保存图形验证码"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                expires_at = time.time() + (expires_minutes * 60)

                # 删除该session的旧验证码
                cursor.execute('DELETE FROM captcha_codes WHERE session_id = ?', (session_id,))

                cursor.execute('''
                INSERT INTO captcha_codes (session_id, code, expires_at)
                VALUES (?, ?, ?)
                ''', (session_id, captcha_text.upper(), expires_at))

                self.conn.commit()
                logger.debug(f"保存图形验证码成功: {session_id}")
                return True
            except Exception as e:
                logger.error(f"保存图形验证码失败: {e}")
                self.conn.rollback()
                return False

    def verify_captcha(self, session_id: str, user_input: str) -> bool:
        """验证图形验证码"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                current_time = time.time()

                # 查找有效的验证码
                cursor.execute('''
                SELECT id FROM captcha_codes
                WHERE session_id = ? AND code = ? AND expires_at > ?
                ORDER BY created_at DESC LIMIT 1
                ''', (session_id, user_input.upper(), current_time))

                row = cursor.fetchone()
                if row:
                    # 删除已使用的验证码
                    cursor.execute('DELETE FROM captcha_codes WHERE id = ?', (row[0],))
                    self.conn.commit()
                    logger.debug(f"图形验证码验证成功: {session_id}")
                    return True
                else:
                    logger.warning(f"图形验证码验证失败: {session_id} - {user_input}")
                    return False
            except Exception as e:
                logger.error(f"验证图形验证码失败: {e}")
                return False

    def save_verification_code(self, email: str, code: str, code_type: str = 'register', expires_minutes: int = 10) -> bool:
        """保存邮箱验证码"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                expires_at = time.time() + (expires_minutes * 60)

                cursor.execute('''
                INSERT INTO email_verifications (email, code, type, expires_at)
                VALUES (?, ?, ?, ?)
                ''', (email, code, code_type, expires_at))

                self.conn.commit()
                logger.info(f"保存验证码成功: {email} ({code_type})")
                return True
            except Exception as e:
                logger.error(f"保存验证码失败: {e}")
                self.conn.rollback()
                return False

    def verify_email_code(self, email: str, code: str, code_type: str = 'register') -> bool:
        """验证邮箱验证码"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                current_time = time.time()

                # 查找有效的验证码
                cursor.execute('''
                SELECT id FROM email_verifications
                WHERE email = ? AND code = ? AND type = ? AND expires_at > ? AND used = FALSE
                ORDER BY created_at DESC LIMIT 1
                ''', (email, code, code_type, current_time))

                row = cursor.fetchone()
                if row:
                    # 标记验证码为已使用
                    cursor.execute('''
                    UPDATE email_verifications SET used = TRUE WHERE id = ?
                    ''', (row[0],))
                    self.conn.commit()
                    logger.info(f"验证码验证成功: {email} ({code_type})")
                    return True
                else:
                    logger.warning(f"验证码验证失败: {email} - {code} ({code_type})")
                    return False
            except Exception as e:
                logger.error(f"验证邮箱验证码失败: {e}")
                return False

    async def send_verification_email(self, email: str, code: str) -> bool:
        """发送验证码邮件（支持SMTP和API两种方式）"""
        try:
            subject = "闲鱼管理系统 - 邮箱验证码"
            # 使用简单的纯文本邮件内容
            text_content = f"""【闲鱼管理系统】邮箱验证码

您好！

感谢您使用闲鱼管理系统。为了确保账户安全，请使用以下验证码完成邮箱验证：

验证码：{code}

重要提醒：
• 验证码有效期为 10 分钟，请及时使用
• 请勿将验证码分享给任何人
• 如非本人操作，请忽略此邮件
• 系统不会主动索要您的验证码

感谢您选择闲鱼管理系统！

---
此邮件由系统自动发送，请勿直接回复
© 2026 闲鱼管理系统"""

            # 从系统设置读取SMTP配置
            try:
                smtp_server = self.get_system_setting('smtp_server') or ''
                smtp_port = int(self.get_system_setting('smtp_port') or 0)
                smtp_user = self.get_system_setting('smtp_user') or ''
                smtp_password = self.get_system_setting('smtp_password') or ''
                smtp_from = (self.get_system_setting('smtp_from') or '').strip() or smtp_user
                smtp_use_tls = (self.get_system_setting('smtp_use_tls') or 'true').lower() == 'true'
                smtp_use_ssl = (self.get_system_setting('smtp_use_ssl') or 'false').lower() == 'true'
            except Exception as e:
                logger.error(f"读取SMTP系统设置失败: {e}")
                # 如果读取配置失败，使用API方式
                return await self._send_email_via_api(email, subject, text_content)

            # 检查SMTP配置是否完整
            if smtp_server and smtp_port and smtp_user and smtp_password:
                # 配置完整，使用SMTP方式发送
                logger.info(f"使用SMTP方式发送验证码邮件: {email}")
                return await self._send_email_via_smtp(email, subject, text_content,
                                                     smtp_server, smtp_port, smtp_user,
                                                     smtp_password, smtp_from, smtp_use_tls, smtp_use_ssl)
            else:
                # 配置不完整，使用API方式发送
                logger.info(f"SMTP配置不完整，使用API方式发送验证码邮件: {email}")
                return await self._send_email_via_api(email, subject, text_content)

        except Exception as e:
            logger.error(f"发送验证码邮件异常: {e}")
            return False

    async def _send_email_via_smtp(self, email: str, subject: str, text_content: str,
                                 smtp_server: str, smtp_port: int, smtp_user: str,
                                 smtp_password: str, smtp_from: str, smtp_use_tls: bool, smtp_use_ssl: bool) -> bool:
        """使用SMTP方式发送邮件"""
        try:
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart

            msg = MIMEMultipart()
            msg['Subject'] = subject
            msg['From'] = smtp_from
            msg['To'] = email

            msg.attach(MIMEText(text_content, 'plain', 'utf-8'))

            if smtp_use_ssl:
                server = smtplib.SMTP_SSL(smtp_server, smtp_port)
            else:
                server = smtplib.SMTP(smtp_server, smtp_port)

            server.ehlo()
            if smtp_use_tls and not smtp_use_ssl:
                server.starttls()
                server.ehlo()

            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, [email], msg.as_string())
            server.quit()

            logger.info(f"验证码邮件发送成功(SMTP): {email}")
            return True
        except Exception as e:
            logger.error(f"SMTP发送验证码邮件失败: {e}")
            # SMTP发送失败，尝试使用API方式
            logger.info(f"SMTP发送失败，尝试使用API方式发送: {email}")
            return await self._send_email_via_api(email, subject, text_content)

    async def _send_email_via_api(self, email: str, subject: str, text_content: str) -> bool:
        """使用用户显式配置的邮件API发送邮件。"""
        try:
            import aiohttp

            api_url = (
                (self.get_system_setting('verification_email_api_url') or '').strip()
                or str(os.getenv('VERIFICATION_EMAIL_API_URL') or '').strip()
            )
            if not api_url:
                logger.warning(f"未配置验证码邮件API地址，无法通过API发送验证码邮件: {email}")
                return False

            params = {
                'subject': subject,
                'receiveUser': email,
                'sendHtml': text_content
            }

            async with aiohttp.ClientSession() as session:
                try:
                    logger.info(f"使用API发送验证码邮件: {email}")
                    async with session.get(api_url, params=params, timeout=15) as response:
                        response_text = await response.text()
                        logger.info(f"邮件API响应: {response.status}")

                        if response.status == 200:
                            logger.info(f"验证码邮件发送成功(API): {email}")
                            return True
                        else:
                            logger.error(f"API发送验证码邮件失败: {email}, 状态码: {response.status}, 响应: {response_text[:200]}")
                            return False
                except Exception as e:
                    logger.error(f"API邮件发送异常: {email}, 错误: {e}")
                    return False
        except Exception as e:
            logger.error(f"API邮件发送方法异常: {e}")
            return False

    # ==================== 卡券管理方法 ====================

    def _normalize_card_name(self, name: Any) -> str:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("卡券名称不能为空")
        return normalized_name

    def _normalize_card_type(self, card_type: Any) -> str:
        normalized_card_type = str(card_type or "").strip()
        allowed_card_types = {'api', 'yifan_api', 'text', 'data', 'image'}
        if normalized_card_type not in allowed_card_types:
            raise ValueError("卡券类型无效")
        return normalized_card_type

    def create_card(self, name: str, card_type: str, api_config=None,
                   text_content: str = None, data_content: str = None, image_url: str = None,
                   description: str = None, enabled: bool = True, delay_seconds: int = 0,
                   is_multi_spec: bool = False, spec_name: str = None, spec_value: str = None,
                   spec_name_2: str = None, spec_value_2: str = None, user_id: int = None):
        """创建新卡券（支持双规格）"""
        normalized_name = self._normalize_card_name(name)
        normalized_card_type = self._normalize_card_type(card_type)

        with self.lock:
            try:
                # 验证多规格参数
                if is_multi_spec:
                    if not spec_name or not spec_value:
                        raise ValueError("多规格卡券必须提供规格名称和规格值")

                    # 检查唯一性：卡券名称+规格名称+规格值
                    cursor = self.conn.cursor()
                    cursor.execute('''
                    SELECT COUNT(*) FROM cards
                    WHERE name = ? AND spec_name = ? AND spec_value = ? AND user_id = ?
                    ''', (normalized_name, spec_name, spec_value, user_id))

                    if cursor.fetchone()[0] > 0:
                        raise ValueError(f"卡券已存在：{normalized_name} - {spec_name}:{spec_value}")
                else:
                    # 检查唯一性：仅卡券名称
                    cursor = self.conn.cursor()
                    cursor.execute('''
                    SELECT COUNT(*) FROM cards
                    WHERE name = ? AND (is_multi_spec = 0 OR is_multi_spec IS NULL) AND user_id = ?
                    ''', (normalized_name, user_id))

                    if cursor.fetchone()[0] > 0:
                        raise ValueError(f"卡券名称已存在：{normalized_name}")

                # 处理api_config参数 - 如果是字典则转换为JSON字符串
                api_config_str = None
                if api_config is not None:
                    if isinstance(api_config, dict):
                        import json
                        api_config_str = json.dumps(api_config)
                    else:
                        api_config_str = str(api_config)

                cursor.execute('''
                INSERT INTO cards (name, type, api_config, text_content, data_content, image_url,
                                 description, enabled, delay_seconds, is_multi_spec,
                                 spec_name, spec_value, spec_name_2, spec_value_2, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (normalized_name, normalized_card_type, api_config_str, text_content, data_content, image_url,
                      description, enabled, delay_seconds, is_multi_spec,
                      spec_name, spec_value, spec_name_2, spec_value_2, user_id))
                self.conn.commit()
                card_id = cursor.lastrowid

                if is_multi_spec:
                    logger.info(f"创建多规格卡券成功: {normalized_name} - {spec_name}:{spec_value} (ID: {card_id})")
                else:
                    logger.info(f"创建卡券成功: {normalized_name} (ID: {card_id})")
                return card_id
            except Exception as e:
                logger.error(f"创建卡券失败: {e}")
                raise

    def get_all_cards(self, user_id: int = None, summary_only: bool = False):
        """获取所有卡券（支持用户隔离）"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if summary_only:
                    select_sql = '''
                    SELECT id, name, type, data_content, description, enabled, delay_seconds, is_multi_spec,
                           spec_name, spec_value, spec_name_2, spec_value_2, created_at, updated_at
                    FROM cards
                    '''
                    if user_id is not None:
                        cursor.execute(select_sql + "WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
                    else:
                        cursor.execute(select_sql + "ORDER BY created_at DESC")
                else:
                    if user_id is not None:
                        cursor.execute('''
                        SELECT id, name, type, api_config, text_content, data_content, image_url,
                               description, enabled, delay_seconds, is_multi_spec,
                               spec_name, spec_value, spec_name_2, spec_value_2, created_at, updated_at
                        FROM cards
                        WHERE user_id = ?
                        ORDER BY created_at DESC
                        ''', (user_id,))
                    else:
                        cursor.execute('''
                        SELECT id, name, type, api_config, text_content, data_content, image_url,
                               description, enabled, delay_seconds, is_multi_spec,
                               spec_name, spec_value, spec_name_2, spec_value_2, created_at, updated_at
                        FROM cards
                        ORDER BY created_at DESC
                        ''')

                cards = []
                for row in cursor.fetchall():
                    if summary_only:
                        raw_data_content = str(row[3] or '')
                        data_count = len([line for line in raw_data_content.splitlines() if line.strip()])
                        cards.append({
                            'id': row[0],
                            'name': row[1],
                            'type': row[2],
                            'description': row[4],
                            'enabled': bool(row[5]),
                            'delay_seconds': row[6] or 0,
                            'is_multi_spec': bool(row[7]) if row[7] is not None else False,
                            'spec_name': row[8],
                            'spec_value': row[9],
                            'spec_name_2': row[10],
                            'spec_value_2': row[11],
                            'created_at': row[12],
                            'updated_at': row[13],
                            'data_count': data_count,
                        })
                    else:
                        # 解析api_config JSON字符串
                        api_config = row[3]
                        if api_config:
                            try:
                                import json
                                api_config = json.loads(api_config)
                            except (json.JSONDecodeError, TypeError):
                                # 如果解析失败，保持原始字符串
                                pass

                        cards.append({
                            'id': row[0],
                            'name': row[1],
                            'type': row[2],
                            'api_config': api_config,
                            'text_content': row[4],
                            'data_content': row[5],
                            'image_url': row[6],
                            'description': row[7],
                            'enabled': bool(row[8]),
                            'delay_seconds': row[9] or 0,
                            'is_multi_spec': bool(row[10]) if row[10] is not None else False,
                            'spec_name': row[11],
                            'spec_value': row[12],
                            'spec_name_2': row[13],
                            'spec_value_2': row[14],
                            'created_at': row[15],
                            'updated_at': row[16]
                        })

                return cards
            except Exception as e:
                logger.error(f"获取卡券列表失败: {e}")
                raise

    def get_card_by_id(self, card_id: int, user_id: int = None):
        """根据ID获取卡券（支持用户隔离）"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    cursor.execute('''
                    SELECT id, name, type, api_config, text_content, data_content, image_url,
                           description, enabled, delay_seconds, is_multi_spec,
                           spec_name, spec_value, spec_name_2, spec_value_2, created_at, updated_at
                    FROM cards WHERE id = ? AND user_id = ?
                    ''', (card_id, user_id))
                else:
                    cursor.execute('''
                    SELECT id, name, type, api_config, text_content, data_content, image_url,
                           description, enabled, delay_seconds, is_multi_spec,
                           spec_name, spec_value, spec_name_2, spec_value_2, created_at, updated_at
                    FROM cards WHERE id = ?
                    ''', (card_id,))

                row = cursor.fetchone()
                if row:
                    # 解析api_config JSON字符串
                    api_config = row[3]
                    if api_config:
                        try:
                            import json
                            api_config = json.loads(api_config)
                        except (json.JSONDecodeError, TypeError):
                            # 如果解析失败，保持原始字符串
                            pass

                    return {
                        'id': row[0],
                        'name': row[1],
                        'type': row[2],
                        'api_config': api_config,
                        'text_content': row[4],
                        'data_content': row[5],
                        'image_url': row[6],
                        'description': row[7],
                        'enabled': bool(row[8]),
                        'delay_seconds': row[9] or 0,
                        'is_multi_spec': bool(row[10]) if row[10] is not None else False,
                        'spec_name': row[11],
                        'spec_value': row[12],
                        'spec_name_2': row[13],
                        'spec_value_2': row[14],
                        'created_at': row[15],
                        'updated_at': row[16]
                    }
                return None
            except Exception as e:
                logger.error(f"获取卡券失败: {e}")
                raise

    def update_card(self, card_id: int, name: str = None, card_type: str = None,
                   api_config=None, text_content: str = None, data_content: str = None,
                   image_url: str = None, description: str = None, enabled: bool = None,
                   delay_seconds: int = None, is_multi_spec: bool = None, spec_name: str = None,
                   spec_value: str = None, spec_name_2: str = None, spec_value_2: str = None,
                   user_id: int = None):
        """更新卡券（支持用户隔离）"""
        with self.lock:
            try:
                # 处理api_config参数
                api_config_str = None
                if api_config is not None:
                    if isinstance(api_config, dict):
                        import json
                        api_config_str = json.dumps(api_config)
                    else:
                        api_config_str = str(api_config)

                cursor = self.conn.cursor()

                if user_id is not None:
                    cursor.execute(
                        '''
                        SELECT name, is_multi_spec, spec_name, spec_value, spec_name_2, spec_value_2
                        FROM cards WHERE id = ? AND user_id = ?
                        ''',
                        (card_id, user_id),
                    )
                else:
                    cursor.execute(
                        '''
                        SELECT name, is_multi_spec, spec_name, spec_value, spec_name_2, spec_value_2
                        FROM cards WHERE id = ?
                        ''',
                        (card_id,),
                    )

                current_card = cursor.fetchone()
                if not current_card:
                    return False

                target_name = self._normalize_card_name(name) if name is not None else current_card[0]
                target_is_multi_spec = (
                    bool(is_multi_spec)
                    if is_multi_spec is not None
                    else bool(current_card[1]) if current_card[1] is not None else False
                )
                target_spec_name = spec_name if spec_name is not None else current_card[2]
                target_spec_value = spec_value if spec_value is not None else current_card[3]

                should_clear_spec_fields = False
                if target_is_multi_spec:
                    if not target_spec_name or not target_spec_value:
                        raise ValueError("多规格卡券必须提供规格名称和规格值")

                    if user_id is not None:
                        cursor.execute(
                            '''
                            SELECT COUNT(*) FROM cards
                            WHERE name = ? AND spec_name = ? AND spec_value = ? AND user_id = ? AND id != ?
                            ''',
                            (target_name, target_spec_name, target_spec_value, user_id, card_id),
                        )
                    else:
                        cursor.execute(
                            '''
                            SELECT COUNT(*) FROM cards
                            WHERE name = ? AND spec_name = ? AND spec_value = ? AND id != ?
                            ''',
                            (target_name, target_spec_name, target_spec_value, card_id),
                        )

                    if cursor.fetchone()[0] > 0:
                        raise ValueError(f"卡券已存在：{target_name} - {target_spec_name}:{target_spec_value}")
                else:
                    should_clear_spec_fields = True
                    spec_name = None
                    spec_value = None
                    spec_name_2 = None
                    spec_value_2 = None

                    if user_id is not None:
                        cursor.execute(
                            '''
                            SELECT COUNT(*) FROM cards
                            WHERE name = ? AND (is_multi_spec = 0 OR is_multi_spec IS NULL) AND user_id = ? AND id != ?
                            ''',
                            (target_name, user_id, card_id),
                        )
                    else:
                        cursor.execute(
                            '''
                            SELECT COUNT(*) FROM cards
                            WHERE name = ? AND (is_multi_spec = 0 OR is_multi_spec IS NULL) AND id != ?
                            ''',
                            (target_name, card_id),
                        )

                    if cursor.fetchone()[0] > 0:
                        raise ValueError(f"卡券名称已存在：{target_name}")

                # 构建更新语句
                update_fields = []
                params = []

                if name is not None:
                    update_fields.append("name = ?")
                    params.append(target_name)
                if card_type is not None:
                    normalized_card_type = self._normalize_card_type(card_type)
                    update_fields.append("type = ?")
                    params.append(normalized_card_type)
                if api_config_str is not None:
                    update_fields.append("api_config = ?")
                    params.append(api_config_str)
                if text_content is not None:
                    update_fields.append("text_content = ?")
                    params.append(text_content)
                if data_content is not None:
                    update_fields.append("data_content = ?")
                    params.append(data_content)
                if image_url is not None:
                    update_fields.append("image_url = ?")
                    params.append(image_url)
                if description is not None:
                    update_fields.append("description = ?")
                    params.append(description)
                if enabled is not None:
                    update_fields.append("enabled = ?")
                    params.append(enabled)
                if delay_seconds is not None:
                    update_fields.append("delay_seconds = ?")
                    params.append(delay_seconds)
                if is_multi_spec is not None:
                    update_fields.append("is_multi_spec = ?")
                    params.append(is_multi_spec)
                if spec_name is not None or should_clear_spec_fields:
                    update_fields.append("spec_name = ?")
                    params.append(spec_name)
                if spec_value is not None or should_clear_spec_fields:
                    update_fields.append("spec_value = ?")
                    params.append(spec_value)
                if spec_name_2 is not None or should_clear_spec_fields:
                    update_fields.append("spec_name_2 = ?")
                    params.append(spec_name_2)
                if spec_value_2 is not None or should_clear_spec_fields:
                    update_fields.append("spec_value_2 = ?")
                    params.append(spec_value_2)

                if not update_fields:
                    return True  # 没有需要更新的字段

                update_fields.append("updated_at = CURRENT_TIMESTAMP")
                params.append(card_id)

                if user_id is not None:
                    params.append(user_id)
                    sql = f"UPDATE cards SET {', '.join(update_fields)} WHERE id = ? AND user_id = ?"
                else:
                    sql = f"UPDATE cards SET {', '.join(update_fields)} WHERE id = ?"

                self._execute_sql(cursor, sql, params)

                if cursor.rowcount > 0:
                    self.conn.commit()
                    logger.info(f"更新卡券成功: ID {card_id}")
                    return True
                else:
                    return False  # 没有找到对应的记录

            except Exception as e:
                logger.error(f"更新卡券失败: {e}")
                self.conn.rollback()
                raise

    def update_card_image_url(self, card_id: int, new_image_url: str) -> bool:
        """更新卡券的图片URL"""
        with self.lock:
            try:
                cursor = self.conn.cursor()

                # 更新图片URL
                self._execute_sql(cursor,
                    "UPDATE cards SET image_url = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND type = 'image'",
                    (new_image_url, card_id))

                self.conn.commit()

                # 检查是否有行被更新
                if cursor.rowcount > 0:
                    logger.info(f"卡券图片URL更新成功: 卡券ID: {card_id}, 新URL: {new_image_url}")
                    return True
                else:
                    logger.warning(f"未找到匹配的图片卡券: 卡券ID: {card_id}")
                    return False

            except Exception as e:
                logger.error(f"更新卡券图片URL失败: {e}")
                self.conn.rollback()
                return False

    # ==================== 自动发货规则方法 ====================

    def _normalize_delivery_rule_count(self, delivery_count: Any) -> int:
        try:
            normalized_count = int(delivery_count)
        except (TypeError, ValueError) as exc:
            raise ValueError("发货数量必须为大于等于 1 的整数") from exc
        if normalized_count < 1:
            raise ValueError("发货数量必须为大于等于 1 的整数")
        return normalized_count

    def _normalize_delivery_rule_keyword(self, keyword: Any) -> str:
        normalized_keyword = str(keyword or "").strip()
        if not normalized_keyword:
            raise ValueError("发货规则关键词不能为空")
        return normalized_keyword

    def create_delivery_rule(self, keyword: str, card_id: int, delivery_count: int = 1,
                           enabled: bool = True, description: str = None, user_id: int = None):
        """创建发货规则"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                normalized_keyword = self._normalize_delivery_rule_keyword(keyword)
                normalized_delivery_count = self._normalize_delivery_rule_count(delivery_count)

                if user_id is not None and card_id is not None:
                    self._execute_sql(cursor, '''
                    SELECT 1 FROM cards WHERE id = ? AND user_id = ?
                    ''', (card_id, user_id))
                    if not cursor.fetchone():
                        raise ValueError(f"卡券不存在或无权限访问: {card_id}")

                cursor.execute('''
                INSERT INTO delivery_rules (keyword, card_id, delivery_count, enabled, description, user_id)
                VALUES (?, ?, ?, ?, ?, ?)
                ''', (normalized_keyword, card_id, normalized_delivery_count, enabled, description, user_id))
                self.conn.commit()
                rule_id = cursor.lastrowid
                logger.info(f"创建发货规则成功: {normalized_keyword} -> 卡券ID {card_id} (规则ID: {rule_id})")
                return rule_id
            except Exception as e:
                logger.error(f"创建发货规则失败: {e}")
                raise

    def get_all_delivery_rules(self, user_id: int = None):
        """获取所有发货规则"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    cursor.execute('''
                    SELECT dr.id, dr.keyword, dr.card_id, dr.delivery_count, dr.enabled,
                           dr.description, dr.delivery_times, dr.created_at, dr.updated_at,
                           c.name as card_name, c.type as card_type, c.enabled as card_enabled,
                           c.is_multi_spec, c.spec_name, c.spec_value,
                           c.spec_name_2, c.spec_value_2
                    FROM delivery_rules dr
                    LEFT JOIN cards c ON dr.card_id = c.id
                    WHERE dr.user_id = ?
                    ORDER BY dr.created_at DESC
                    ''', (user_id,))
                else:
                    cursor.execute('''
                    SELECT dr.id, dr.keyword, dr.card_id, dr.delivery_count, dr.enabled,
                           dr.description, dr.delivery_times, dr.created_at, dr.updated_at,
                           c.name as card_name, c.type as card_type, c.enabled as card_enabled,
                           c.is_multi_spec, c.spec_name, c.spec_value,
                           c.spec_name_2, c.spec_value_2
                    FROM delivery_rules dr
                    LEFT JOIN cards c ON dr.card_id = c.id
                    ORDER BY dr.created_at DESC
                    ''')

                rules = []
                for row in cursor.fetchall():
                    rules.append({
                        'id': row[0],
                        'keyword': row[1],
                        'card_id': row[2],
                        'delivery_count': row[3],
                        'enabled': bool(row[4]),
                        'description': row[5],
                        'delivery_times': row[6],
                        'created_at': row[7],
                        'updated_at': row[8],
                        'card_name': row[9],
                        'card_type': row[10],
                        'card_enabled': bool(row[11]) if row[11] is not None else False,
                        'is_multi_spec': bool(row[12]) if row[12] is not None else False,
                        'spec_name': row[13],
                        'spec_value': row[14],
                        'spec_name_2': row[15],
                        'spec_value_2': row[16]
                    })

                return rules
            except Exception as e:
                logger.error(f"获取发货规则列表失败: {e}")
                raise

    def get_delivery_rules_by_keyword(self, keyword: str, user_id: int = None, only_non_multi_spec: bool = False):
        """根据关键字获取匹配的发货规则

        Args:
            keyword: 搜索关键字（商品标题）
            user_id: 用户ID，用于过滤只属于该用户的发货规则
            only_non_multi_spec: 是否仅返回普通卡券规则（排除多规格卡券）
        """
        with self.lock:
            try:
                cursor = self.conn.cursor()
                non_multi_filter = "AND (c.is_multi_spec = 0 OR c.is_multi_spec IS NULL)" if only_non_multi_spec else ""
                # 使用更灵活的匹配方式：既支持商品内容包含关键字，也支持关键字包含在商品内容中
                if user_id is not None:
                    cursor.execute(f'''
                    SELECT dr.id, dr.keyword, dr.card_id, dr.delivery_count, dr.enabled,
                           dr.description, dr.delivery_times,
                           c.name as card_name, c.type as card_type, c.api_config,
                           c.text_content, c.data_content, c.image_url, c.enabled as card_enabled, c.description as card_description,
                           c.delay_seconds as card_delay_seconds,
                           c.is_multi_spec, c.spec_name, c.spec_value, c.spec_name_2, c.spec_value_2
                    FROM delivery_rules dr
                    LEFT JOIN cards c ON dr.card_id = c.id
                    WHERE dr.enabled = 1 AND c.enabled = 1 AND dr.user_id = ?
                    AND (? LIKE '%' || dr.keyword || '%' OR dr.keyword LIKE '%' || ? || '%')
                    {non_multi_filter}
                    ORDER BY
                        CASE
                            WHEN ? LIKE '%' || dr.keyword || '%' THEN LENGTH(dr.keyword)
                            ELSE LENGTH(dr.keyword) / 2
                        END DESC,
                        dr.id ASC
                    ''', (user_id, keyword, keyword, keyword))
                else:
                    cursor.execute(f'''
                    SELECT dr.id, dr.keyword, dr.card_id, dr.delivery_count, dr.enabled,
                           dr.description, dr.delivery_times,
                           c.name as card_name, c.type as card_type, c.api_config,
                           c.text_content, c.data_content, c.image_url, c.enabled as card_enabled, c.description as card_description,
                           c.delay_seconds as card_delay_seconds,
                           c.is_multi_spec, c.spec_name, c.spec_value, c.spec_name_2, c.spec_value_2
                    FROM delivery_rules dr
                    LEFT JOIN cards c ON dr.card_id = c.id
                    WHERE dr.enabled = 1 AND c.enabled = 1
                    AND (? LIKE '%' || dr.keyword || '%' OR dr.keyword LIKE '%' || ? || '%')
                    {non_multi_filter}
                    ORDER BY
                        CASE
                            WHEN ? LIKE '%' || dr.keyword || '%' THEN LENGTH(dr.keyword)
                            ELSE LENGTH(dr.keyword) / 2
                        END DESC,
                        dr.id ASC
                    ''', (keyword, keyword, keyword))

                rules = []
                for row in cursor.fetchall():
                    # 解析api_config JSON字符串
                    api_config = row[9]
                    if api_config:
                        try:
                            import json
                            api_config = json.loads(api_config)
                        except (json.JSONDecodeError, TypeError):
                            # 如果解析失败，保持原始字符串
                            pass

                    rules.append({
                        'id': row[0],
                        'keyword': row[1],
                        'card_id': row[2],
                        'delivery_count': row[3],
                        'enabled': bool(row[4]),
                        'description': row[5],
                        'delivery_times': row[6],
                        'card_name': row[7],
                        'card_type': row[8],
                        'api_config': api_config,  # 修复字段名
                        'text_content': row[10],
                        'data_content': row[11],
                        'image_url': row[12],
                        'card_enabled': bool(row[13]),
                        'card_description': row[14],  # 卡券备注信息
                        'card_delay_seconds': row[15] or 0,  # 延时秒数
                        'is_multi_spec': bool(row[16]) if row[16] is not None else False,
                        'spec_name': row[17],
                        'spec_value': row[18],
                        'spec_name_2': row[19],
                        'spec_value_2': row[20]
                    })

                return rules
            except Exception as e:
                logger.error(f"根据关键字获取发货规则失败: {e}")
                return []

    def get_delivery_rule_by_id(self, rule_id: int, user_id: int = None):
        """根据ID获取发货规则（支持用户隔离）"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    self._execute_sql(cursor, '''
                    SELECT dr.id, dr.keyword, dr.card_id, dr.delivery_count, dr.enabled,
                           dr.description, dr.delivery_times, dr.created_at, dr.updated_at,
                           c.name as card_name, c.type as card_type,
                           c.is_multi_spec, c.spec_name, c.spec_value,
                           c.spec_name_2, c.spec_value_2
                    FROM delivery_rules dr
                    LEFT JOIN cards c ON dr.card_id = c.id
                    WHERE dr.id = ? AND dr.user_id = ?
                    ''', (rule_id, user_id))
                else:
                    self._execute_sql(cursor, '''
                    SELECT dr.id, dr.keyword, dr.card_id, dr.delivery_count, dr.enabled,
                           dr.description, dr.delivery_times, dr.created_at, dr.updated_at,
                           c.name as card_name, c.type as card_type,
                           c.is_multi_spec, c.spec_name, c.spec_value,
                           c.spec_name_2, c.spec_value_2
                    FROM delivery_rules dr
                    LEFT JOIN cards c ON dr.card_id = c.id
                    WHERE dr.id = ?
                    ''', (rule_id,))

                row = cursor.fetchone()
                if row:
                    return {
                        'id': row[0],
                        'keyword': row[1],
                        'card_id': row[2],
                        'delivery_count': row[3],
                        'enabled': bool(row[4]),
                        'description': row[5],
                        'delivery_times': row[6],
                        'created_at': row[7],
                        'updated_at': row[8],
                        'card_name': row[9],
                        'card_type': row[10],
                        'is_multi_spec': bool(row[11]) if row[11] is not None else False,
                        'spec_name': row[12],
                        'spec_value': row[13],
                        'spec_name_2': row[14],
                        'spec_value_2': row[15]
                    }
                return None
            except Exception as e:
                logger.error(f"获取发货规则失败: {e}")
                raise

    def update_delivery_rule(self, rule_id: int, keyword: str = None, card_id: int = None,
                           delivery_count: int = None, enabled: bool = None,
                           description: str = None, user_id: int = None):
        """更新发货规则（支持用户隔离）"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                normalized_keyword = None
                if keyword is not None:
                    normalized_keyword = self._normalize_delivery_rule_keyword(keyword)
                normalized_delivery_count = None
                if delivery_count is not None:
                    normalized_delivery_count = self._normalize_delivery_rule_count(delivery_count)

                if user_id is not None and card_id is not None:
                    self._execute_sql(cursor, '''
                    SELECT 1 FROM cards WHERE id = ? AND user_id = ?
                    ''', (card_id, user_id))
                    if not cursor.fetchone():
                        raise ValueError(f"卡券不存在或无权限访问: {card_id}")

                # 构建更新语句
                update_fields = []
                params = []

                if keyword is not None:
                    update_fields.append("keyword = ?")
                    params.append(normalized_keyword)
                if card_id is not None:
                    update_fields.append("card_id = ?")
                    params.append(card_id)
                if delivery_count is not None:
                    update_fields.append("delivery_count = ?")
                    params.append(normalized_delivery_count)
                if enabled is not None:
                    update_fields.append("enabled = ?")
                    params.append(enabled)
                if description is not None:
                    update_fields.append("description = ?")
                    params.append(description)

                if not update_fields:
                    return True  # 没有需要更新的字段

                update_fields.append("updated_at = CURRENT_TIMESTAMP")
                params.append(rule_id)

                if user_id is not None:
                    params.append(user_id)
                    sql = f"UPDATE delivery_rules SET {', '.join(update_fields)} WHERE id = ? AND user_id = ?"
                else:
                    sql = f"UPDATE delivery_rules SET {', '.join(update_fields)} WHERE id = ?"

                self._execute_sql(cursor, sql, params)

                if cursor.rowcount > 0:
                    self.conn.commit()
                    logger.info(f"更新发货规则成功: ID {rule_id}")
                    return True
                else:
                    return False  # 没有找到对应的记录

            except Exception as e:
                logger.error(f"更新发货规则失败: {e}")
                self.conn.rollback()
                raise

    def increment_delivery_times(self, rule_id: int):
        """增加发货次数（同时更新今日发货次数）"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                today = datetime.now().strftime('%Y-%m-%d')

                # 先查询当前规则的最后发货日期
                cursor.execute('SELECT last_delivery_date FROM delivery_rules WHERE id = ?', (rule_id,))
                row = cursor.fetchone()
                last_date = row[0] if row else None

                if last_date == today:
                    # 今天已有发货记录，增加今日发货次数
                    cursor.execute('''
                    UPDATE delivery_rules
                    SET delivery_times = delivery_times + 1,
                        today_delivery_times = today_delivery_times + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    ''', (rule_id,))
                else:
                    # 新的一天，重置今日发货次数为1
                    cursor.execute('''
                    UPDATE delivery_rules
                    SET delivery_times = delivery_times + 1,
                        last_delivery_date = ?,
                        today_delivery_times = 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    ''', (today, rule_id))

                self.conn.commit()
                logger.debug(f"发货规则 {rule_id} 发货次数已增加")
            except Exception as e:
                logger.error(f"更新发货次数失败: {e}")

    def get_today_delivery_count(self, user_id: int = None):
        """获取今日发货总数"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                today = datetime.now().strftime('%Y-%m-%d')

                if user_id is not None:
                    cursor.execute('''
                    SELECT COALESCE(SUM(today_delivery_times), 0)
                    FROM delivery_rules
                    WHERE last_delivery_date = ? AND user_id = ?
                    ''', (today, user_id))
                else:
                    cursor.execute('''
                    SELECT COALESCE(SUM(today_delivery_times), 0)
                    FROM delivery_rules
                    WHERE last_delivery_date = ?
                    ''', (today,))

                row = cursor.fetchone()
                return row[0] if row else 0
            except Exception as e:
                logger.error(f"获取今日发货统计失败: {e}")
                raise







    def get_delivery_rules_by_keyword_and_spec(self, keyword: str, spec_name: str = None, spec_value: str = None,
                                               spec_name_2: str = None, spec_value_2: str = None, user_id: int = None,
                                               expected_mode: str = None):
        """根据关键字和规格信息获取匹配的发货规则（支持双规格）

        Args:
            keyword: 搜索关键字（商品标题）
            spec_name: 规格1名称
            spec_value: 规格1值
            spec_name_2: 规格2名称
            spec_value_2: 规格2值
            user_id: 用户ID，用于过滤只属于该用户的发货规则
            expected_mode: 期望规则模式，可选 one_spec 或 two_spec
        """
        with self.lock:
            try:
                cursor = self.conn.cursor()

                # 构建user_id过滤条件
                user_filter = "AND dr.user_id = ?" if user_id is not None else ""

                def _normalize_spec_for_match(value: str) -> str:
                    """规格匹配标准化：忽略大小写、前后空白、半角/全角空格差异。"""
                    if value is None:
                        return ''
                    return str(value).strip().lower().replace(' ', '').replace('　', '')

                normalized_spec_name = _normalize_spec_for_match(spec_name)
                normalized_spec_value = _normalize_spec_for_match(spec_value)
                normalized_spec_name_2 = _normalize_spec_for_match(spec_name_2)
                normalized_spec_value_2 = _normalize_spec_for_match(spec_value_2)

                if not normalized_spec_name or not normalized_spec_value:
                    logger.info(f"规格参数不完整，跳过规格匹配: {keyword}")
                    return []

                if expected_mode is None:
                    expected_mode = 'two_spec' if (normalized_spec_name_2 and normalized_spec_value_2) else 'one_spec'

                if expected_mode not in {'one_spec', 'two_spec'}:
                    logger.warning(f"未知的规格匹配模式: {expected_mode}")
                    return []

                if expected_mode == 'two_spec':
                    if not (normalized_spec_name_2 and normalized_spec_value_2):
                        logger.info(f"期望两组规格匹配但订单规格不完整: {keyword}")
                        return []

                    sql = f'''
                    SELECT dr.id, dr.keyword, dr.card_id, dr.delivery_count, dr.enabled,
                           dr.description, dr.delivery_times,
                           c.name as card_name, c.type as card_type, c.api_config,
                           c.text_content, c.data_content, c.enabled as card_enabled,
                           c.description as card_description, c.delay_seconds as card_delay_seconds,
                           c.is_multi_spec, c.spec_name, c.spec_value, c.spec_name_2, c.spec_value_2
                    FROM delivery_rules dr
                    LEFT JOIN cards c ON dr.card_id = c.id
                    WHERE dr.enabled = 1 AND c.enabled = 1 {user_filter}
                    AND (? LIKE '%' || dr.keyword || '%' OR dr.keyword LIKE '%' || ? || '%')
                    AND c.is_multi_spec = 1
                    AND REPLACE(REPLACE(LOWER(TRIM(COALESCE(c.spec_name, ''))), ' ', ''), '　', '') = ?
                    AND REPLACE(REPLACE(LOWER(TRIM(COALESCE(c.spec_value, ''))), ' ', ''), '　', '') = ?
                    AND REPLACE(REPLACE(LOWER(TRIM(COALESCE(c.spec_name_2, ''))), ' ', ''), '　', '') = ?
                    AND REPLACE(REPLACE(LOWER(TRIM(COALESCE(c.spec_value_2, ''))), ' ', ''), '　', '') = ?
                    ORDER BY
                        CASE
                            WHEN ? LIKE '%' || dr.keyword || '%' THEN LENGTH(dr.keyword)
                            ELSE LENGTH(dr.keyword) / 2
                        END DESC,
                        dr.delivery_times ASC
                    '''
                    if user_id is not None:
                        params = [user_id, keyword, keyword, normalized_spec_name, normalized_spec_value,
                                  normalized_spec_name_2, normalized_spec_value_2, keyword]
                    else:
                        params = [keyword, keyword, normalized_spec_name, normalized_spec_value,
                                  normalized_spec_name_2, normalized_spec_value_2, keyword]
                else:
                    sql = f'''
                    SELECT dr.id, dr.keyword, dr.card_id, dr.delivery_count, dr.enabled,
                           dr.description, dr.delivery_times,
                           c.name as card_name, c.type as card_type, c.api_config,
                           c.text_content, c.data_content, c.enabled as card_enabled,
                           c.description as card_description, c.delay_seconds as card_delay_seconds,
                           c.is_multi_spec, c.spec_name, c.spec_value, c.spec_name_2, c.spec_value_2
                    FROM delivery_rules dr
                    LEFT JOIN cards c ON dr.card_id = c.id
                    WHERE dr.enabled = 1 AND c.enabled = 1 {user_filter}
                    AND (? LIKE '%' || dr.keyword || '%' OR dr.keyword LIKE '%' || ? || '%')
                    AND c.is_multi_spec = 1
                    AND REPLACE(REPLACE(LOWER(TRIM(COALESCE(c.spec_name, ''))), ' ', ''), '　', '') = ?
                    AND REPLACE(REPLACE(LOWER(TRIM(COALESCE(c.spec_value, ''))), ' ', ''), '　', '') = ?
                    AND TRIM(COALESCE(c.spec_name_2, '')) = ''
                    AND TRIM(COALESCE(c.spec_value_2, '')) = ''
                    ORDER BY
                        CASE
                            WHEN ? LIKE '%' || dr.keyword || '%' THEN LENGTH(dr.keyword)
                            ELSE LENGTH(dr.keyword) / 2
                        END DESC,
                        dr.delivery_times ASC
                    '''
                    if user_id is not None:
                        params = [user_id, keyword, keyword, normalized_spec_name, normalized_spec_value, keyword]
                    else:
                        params = [keyword, keyword, normalized_spec_name, normalized_spec_value, keyword]

                cursor.execute(sql, params)

                rules = []
                for row in cursor.fetchall():
                    # 解析api_config JSON字符串
                    api_config = row[9]
                    if api_config:
                        try:
                            import json
                            api_config = json.loads(api_config)
                        except (json.JSONDecodeError, TypeError):
                            # 如果解析失败，保持原始字符串
                            pass

                    rules.append({
                        'id': row[0],
                        'keyword': row[1],
                        'card_id': row[2],
                        'delivery_count': row[3],
                        'enabled': bool(row[4]),
                        'description': row[5],
                        'delivery_times': row[6] or 0,
                        'card_name': row[7],
                        'card_type': row[8],
                        'api_config': api_config,
                        'text_content': row[10],
                        'data_content': row[11],
                        'card_enabled': bool(row[12]),
                        'card_description': row[13],
                        'card_delay_seconds': row[14] or 0,
                        'is_multi_spec': bool(row[15]) if row[15] is not None else False,
                        'spec_name': row[16],
                        'spec_value': row[17],
                        'spec_name_2': row[18],
                        'spec_value_2': row[19]
                    })

                if rules:
                    if expected_mode == 'two_spec':
                        logger.info(f"找到两组规格匹配规则: {keyword} - {spec_name}:{spec_value}, {spec_name_2}:{spec_value_2}")
                    else:
                        logger.info(f"找到一组规格匹配规则: {keyword} - {spec_name}:{spec_value}")
                else:
                    if expected_mode == 'two_spec':
                        logger.info(f"未找到两组规格匹配规则: {keyword}")
                    else:
                        logger.info(f"未找到一组规格匹配规则: {keyword}")

                return rules

            except Exception as e:
                logger.error(f"获取发货规则失败: {e}")
                return []

    def delete_card(self, card_id: int, user_id: int = None):
        """删除卡券（支持用户隔离）"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    self._execute_sql(cursor, "DELETE FROM cards WHERE id = ? AND user_id = ?", (card_id, user_id))
                else:
                    self._execute_sql(cursor, "DELETE FROM cards WHERE id = ?", (card_id,))

                if cursor.rowcount > 0:
                    self.conn.commit()
                    logger.info(f"删除卡券成功: ID {card_id} (用户ID: {user_id})")
                    return True
                else:
                    return False  # 没有找到对应的记录

            except Exception as e:
                logger.error(f"删除卡券失败: {e}")
                self.conn.rollback()
                raise

    def delete_delivery_rule(self, rule_id: int, user_id: int = None):
        """删除发货规则（支持用户隔离）"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    self._execute_sql(cursor, "DELETE FROM delivery_rules WHERE id = ? AND user_id = ?", (rule_id, user_id))
                else:
                    self._execute_sql(cursor, "DELETE FROM delivery_rules WHERE id = ?", (rule_id,))

                if cursor.rowcount > 0:
                    self.conn.commit()
                    logger.info(f"删除发货规则成功: ID {rule_id} (用户ID: {user_id})")
                    return True
                else:
                    return False  # 没有找到对应的记录

            except Exception as e:
                logger.error(f"删除发货规则失败: {e}")
                self.conn.rollback()
                raise


    def mark_batch_data_reservation_sent(self, reservation_id: int):
        """标记预占卡密已发送成功。"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(cursor, "SELECT status FROM data_card_reservations WHERE id = ?", (reservation_id,))
                result = cursor.fetchone()
                if not result:
                    return False

                current_status = result[0]
                if current_status in ('sent', 'consumed'):
                    return True
                if current_status != 'reserved':
                    logger.warning(f"批量数据预占状态不允许标记为已发送: reservation_id={reservation_id}, status={current_status}")
                    return False

                self._execute_sql(cursor, '''
                UPDATE data_card_reservations
                SET status = 'sent', sent_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP, expires_at = NULL
                WHERE id = ?
                ''', (reservation_id,))
                self.conn.commit()
                return True
            except Exception as e:
                logger.error(f"标记批量数据预占已发送失败: reservation_id={reservation_id}, error={e}")
                self.conn.rollback()
                return False

    def finalize_batch_data_reservation(self, reservation_id: int):
        """完成批量数据预占，进入 consumed 状态。"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(cursor, "SELECT status FROM data_card_reservations WHERE id = ?", (reservation_id,))
                result = cursor.fetchone()
                if not result:
                    return {'success': False, 'already_finalized': False}

                current_status = result[0]
                if current_status == 'consumed':
                    return {'success': True, 'already_finalized': True}
                if current_status not in ('reserved', 'sent'):
                    logger.warning(f"批量数据预占状态不允许 finalize: reservation_id={reservation_id}, status={current_status}")
                    return {'success': False, 'already_finalized': False}

                self._execute_sql(cursor, '''
                UPDATE data_card_reservations
                SET status = 'consumed', finalized_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP, expires_at = NULL
                WHERE id = ?
                ''', (reservation_id,))
                self.conn.commit()
                return {'success': True, 'already_finalized': False}
            except Exception as e:
                logger.error(f"完成批量数据预占失败: reservation_id={reservation_id}, error={e}")
                self.conn.rollback()
                return {'success': False, 'already_finalized': False}

    def release_batch_data_reservation(self, reservation_id: int, error: str = None, expired: bool = False):
        """释放未发送成功的预占卡密并回滚到卡池头部。"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(cursor, '''
                SELECT card_id, reserved_content, status
                FROM data_card_reservations
                WHERE id = ?
                ''', (reservation_id,))
                result = cursor.fetchone()
                if not result:
                    return False

                card_id, reserved_content, current_status = result
                if current_status in ('released', 'expired'):
                    return True
                if current_status in ('sent', 'consumed'):
                    logger.warning(f"批量数据预占已发送或已完成，不能释放: reservation_id={reservation_id}, status={current_status}")
                    return False

                self._execute_sql(cursor, "SELECT data_content FROM cards WHERE id = ? AND type = 'data'", (card_id,))
                card_row = cursor.fetchone()
                current_content = card_row[0] if card_row and card_row[0] else ''
                new_content = reserved_content if not current_content else f"{reserved_content}\n{current_content}"

                self._execute_sql(cursor, '''
                UPDATE cards
                SET data_content = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                ''', (new_content, card_id))

                next_status = 'expired' if expired else 'released'
                self._execute_sql(cursor, '''
                UPDATE data_card_reservations
                SET status = ?, last_error = ?, released_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP, expires_at = NULL
                WHERE id = ?
                ''', (next_status, error, reservation_id))
                self.conn.commit()
                logger.info(f"释放批量数据预占成功: reservation_id={reservation_id}, status={next_status}")
                return True
            except Exception as e:
                logger.error(f"释放批量数据预占失败: reservation_id={reservation_id}, error={e}")
                self.conn.rollback()
                return False

    def recover_stale_batch_data_reservations(self, ttl_minutes: int = 30):
        """恢复超时未发送的批量数据预占。"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(cursor, '''
                SELECT id FROM data_card_reservations
                WHERE status = 'reserved'
                  AND datetime(created_at) <= datetime('now', ?)
                ORDER BY id ASC
                ''', (f'-{int(ttl_minutes)} minutes',))
                stale_ids = [row[0] for row in cursor.fetchall()]

                recovered = 0
                for reservation_id in stale_ids:
                    if self.release_batch_data_reservation(reservation_id, error='预占超时自动回收', expired=True):
                        recovered += 1

                if recovered:
                    logger.info(f"恢复超时批量数据预占完成: {recovered} 条")
                return recovered
            except Exception as e:
                logger.error(f"恢复超时批量数据预占失败: {e}")
                return 0

    def peek_batch_data(self, card_id: int, line_index: int = 0):
        """预览批量数据指定位置的记录，不执行消费。"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(cursor, "SELECT data_content FROM cards WHERE id = ? AND type = 'data'", (card_id,))
                result = cursor.fetchone()

                if not result or not result[0]:
                    logger.warning(f"卡券 {card_id} 没有批量数据")
                    return None

                data_content = result[0]
                lines = [line.strip() for line in data_content.split('\n') if line.strip()]
                if not lines:
                    logger.warning(f"卡券 {card_id} 批量数据为空")
                    return None

                if line_index < 0 or line_index >= len(lines):
                    logger.warning(f"卡券 {card_id} 预览索引越界: index={line_index}, total={len(lines)}")
                    return None

                logger.info(f"预览批量数据成功: 卡券ID={card_id}, index={line_index}, 剩余={len(lines)}条")
                return lines[line_index]
            except Exception as e:
                logger.error(f"预览批量数据失败: {e}")
                return None

    def consume_specific_batch_data(self, card_id: int, expected_line: str):
        """仅当第一条记录与预期一致时消费批量数据，避免误删其他卡密。"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(cursor, "SELECT data_content FROM cards WHERE id = ? AND type = 'data'", (card_id,))
                result = cursor.fetchone()

                if not result or not result[0]:
                    logger.warning(f"卡券 {card_id} 没有批量数据，无法消费指定记录")
                    return False

                data_content = result[0]
                lines = [line.strip() for line in data_content.split('\n') if line.strip()]
                if not lines:
                    logger.warning(f"卡券 {card_id} 批量数据为空，无法消费指定记录")
                    return False

                first_line = lines[0]
                expected_line = (expected_line or '').strip()
                if not expected_line:
                    logger.warning(f"卡券 {card_id} 缺少预期批量数据内容，拒绝消费")
                    return False

                if first_line != expected_line:
                    logger.warning(
                        f"卡券 {card_id} 批量数据首条与预期不一致，拒绝消费: "
                        f"expected={expected_line!r}, actual={first_line!r}"
                    )
                    return False

                remaining_lines = lines[1:]
                new_data_content = '\n'.join(remaining_lines)

                self._execute_sql(cursor, '''
                UPDATE cards
                SET data_content = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                ''', (new_data_content, card_id))

                self.conn.commit()
                logger.info(f"消费指定批量数据成功: 卡券ID={card_id}, 剩余={len(remaining_lines)}条")
                return True
            except Exception as e:
                logger.error(f"消费指定批量数据失败: {e}")
                self.conn.rollback()
                return False

    def consume_batch_data(self, card_id: int):
        """消费批量数据的第一条记录（线程安全）"""
        with self.lock:
            try:
                cursor = self.conn.cursor()

                # 获取卡券的批量数据
                self._execute_sql(cursor, "SELECT data_content FROM cards WHERE id = ? AND type = 'data'", (card_id,))
                result = cursor.fetchone()

                if not result or not result[0]:
                    logger.warning(f"卡券 {card_id} 没有批量数据")
                    return None

                data_content = result[0]
                lines = [line.strip() for line in data_content.split('\n') if line.strip()]

                if not lines:
                    logger.warning(f"卡券 {card_id} 批量数据为空")
                    return None

                # 获取第一条数据
                first_line = lines[0]

                # 移除第一条数据，更新数据库
                remaining_lines = lines[1:]
                new_data_content = '\n'.join(remaining_lines)

                cursor.execute('''
                UPDATE cards
                SET data_content = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                ''', (new_data_content, card_id))

                self.conn.commit()

                logger.info(f"消费批量数据成功: 卡券ID={card_id}, 剩余={len(remaining_lines)}条")
                return first_line

            except Exception as e:
                logger.error(f"消费批量数据失败: {e}")
                self.conn.rollback()
                return None

    # ==================== 商品信息管理 ====================









    def get_all_items(self) -> List[Dict]:
        """获取所有商品信息

        Returns:
            List[Dict]: 所有商品信息列表
        """
        try:
            with self.lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                SELECT * FROM item_info
                ORDER BY updated_at DESC
                ''')

                columns = [description[0] for description in cursor.description]
                items = []

                for row in cursor.fetchall():
                    item_info = dict(zip(columns, row))

                    # 解析item_detail JSON
                    if item_info.get('item_detail'):
                        try:
                            item_info['item_detail_parsed'] = json.loads(item_info['item_detail'])
                        except:
                            item_info['item_detail_parsed'] = {}

                    items.append(item_info)

                return items

        except Exception as e:
            logger.error(f"获取所有商品信息失败: {e}")
            return []







    # ==================== 用户设置管理方法 ====================

    def get_user_settings(self, user_id: int):
        """获取用户的所有设置"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute('''
                SELECT key, value, description, updated_at
                FROM user_settings
                WHERE user_id = ?
                ORDER BY key
                ''', (user_id,))

                settings = {}
                for row in cursor.fetchall():
                    settings[row[0]] = {
                        'value': row[1],
                        'description': row[2],
                        'updated_at': row[3]
                    }

                return settings
            except Exception as e:
                logger.error(f"获取用户设置失败: {e}")
                raise

    def get_user_setting(self, user_id: int, key: str):
        """获取用户的特定设置"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute('''
                SELECT value, description, updated_at
                FROM user_settings
                WHERE user_id = ? AND key = ?
                ''', (user_id, key))

                row = cursor.fetchone()
                if row:
                    return {
                        'key': key,
                        'value': row[0],
                        'description': row[1],
                        'updated_at': row[2]
                    }
                return None
            except Exception as e:
                logger.error(f"获取用户设置失败: {e}")
                raise

    def set_user_setting(self, user_id: int, key: str, value: str, description: str = None):
        """设置用户配置"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute('''
                INSERT OR REPLACE INTO user_settings (user_id, key, value, description, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (user_id, key, value, description))

                self.conn.commit()
                logger.info(f"用户设置更新成功: user_id={user_id}, key={key}")
                return True
            except Exception as e:
                logger.error(f"设置用户配置失败: {e}")
                self.conn.rollback()
                raise

    def replace_user_menu_settings(
        self,
        user_id: int,
        menu_visibility: str,
        menu_order: str,
    ) -> int:
        """原子替换用户的菜单显示/排序设置。"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.executemany(
                    '''
                    INSERT OR REPLACE INTO user_settings (user_id, key, value, description, updated_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ''',
                    [
                        (user_id, 'menu_visibility', menu_visibility, '菜单显示设置'),
                        (user_id, 'menu_order', menu_order, '菜单顺序设置'),
                    ],
                )
                self.conn.commit()
                logger.info(f"用户菜单设置替换成功: user_id={user_id}")
                return 2
            except Exception as e:
                logger.error(f"替换用户菜单设置失败: user_id={user_id}, error={e}")
                self.conn.rollback()
                raise

    # ==================== 管理员专用方法 ====================

    def get_all_users(self):
        """获取所有用户信息（管理员专用）"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                # 检查is_admin列是否存在
                cursor.execute("PRAGMA table_info(users)")
                columns = [col[1] for col in cursor.fetchall()]
                has_is_admin = 'is_admin' in columns

                if has_is_admin:
                    cursor.execute('''
                    SELECT id, username, email, created_at, updated_at, is_admin
                    FROM users
                    ORDER BY created_at DESC
                    ''')
                else:
                    cursor.execute('''
                    SELECT id, username, email, created_at, updated_at
                    FROM users
                    ORDER BY created_at DESC
                    ''')

                users = []
                for row in cursor.fetchall():
                    user_data = {
                        'id': row[0],
                        'username': row[1],
                        'email': row[2],
                        'created_at': row[3],
                        'updated_at': row[4],
                    }
                    # 设置is_admin: 如果有该列则使用，否则admin用户名默认为管理员
                    if has_is_admin:
                        user_data['is_admin'] = bool(row[5]) if row[5] is not None else (row[1] == 'admin')
                    else:
                        user_data['is_admin'] = (row[1] == 'admin')
                    users.append(user_data)

                return users
            except Exception as e:
                logger.error(f"获取所有用户失败: {e}")
                raise

    def get_user_by_id(self, user_id: int):
        """根据ID获取用户信息"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                # 检查is_admin列是否存在
                cursor.execute("PRAGMA table_info(users)")
                columns = [col[1] for col in cursor.fetchall()]
                has_is_admin = 'is_admin' in columns

                if has_is_admin:
                    cursor.execute('''
                    SELECT id, username, email, created_at, updated_at, is_admin
                    FROM users
                    WHERE id = ?
                    ''', (user_id,))
                else:
                    cursor.execute('''
                    SELECT id, username, email, created_at, updated_at
                    FROM users
                    WHERE id = ?
                    ''', (user_id,))

                row = cursor.fetchone()
                if row:
                    user_data = {
                        'id': row[0],
                        'username': row[1],
                        'email': row[2],
                        'created_at': row[3],
                        'updated_at': row[4],
                    }
                    if has_is_admin:
                        user_data['is_admin'] = bool(row[5]) if row[5] is not None else (row[1] == 'admin')
                    else:
                        user_data['is_admin'] = (row[1] == 'admin')
                    return user_data
                return None
            except Exception as e:
                logger.error(f"获取用户信息失败: {e}")
                raise

    def get_user_id_by_rowid(self, record_id: str) -> Optional[int]:
        """根据 users 表 rowid 获取真实 user_id。"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("SELECT id FROM users WHERE rowid = ?", (record_id,))
                row = cursor.fetchone()
                if not row or row[0] is None:
                    return None
                return int(row[0])
            except Exception as e:
                logger.error(f"根据 rowid 获取用户ID失败: record_id={record_id}, error={e}")
                return None

    def get_system_setting_key_by_rowid(self, record_id: str) -> Optional[str]:
        """根据 system_settings 表 rowid 获取真实 key。"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("SELECT key FROM system_settings WHERE rowid = ?", (record_id,))
                row = cursor.fetchone()
                if not row or row[0] is None:
                    return None
                return str(row[0]).strip()
            except Exception as e:
                logger.error(f"根据 rowid 获取系统设置键失败: record_id={record_id}, error={e}")
                return None

    def update_user_admin_status(self, user_id: int, is_admin: bool) -> bool:
        """更新用户管理员状态"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute('''
                UPDATE users SET is_admin = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                ''', (1 if is_admin else 0, user_id))

                self.conn.commit()
                logger.info(f"用户管理员状态更新成功: user_id={user_id}, is_admin={is_admin}")
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"更新用户管理员状态失败: {e}")
                self.conn.rollback()
                raise

    def delete_user_and_data(self, user_id: int):
        """删除用户及其所有相关数据"""
        with self.lock:
            cursor = None
            try:
                cursor = self.conn.cursor()
                cursor.execute('BEGIN TRANSACTION')
                cursor.execute("SELECT id FROM cookies WHERE user_id = ?", (user_id,))
                user_account_ids = [str(row[0]).strip() for row in cursor.fetchall() if str(row[0] or '').strip()]

                self._delete_user_scoped_rows(cursor, user_id)
                self._delete_account_scoped_rows(cursor, user_account_ids)

                cursor.execute('DELETE FROM cookies WHERE user_id = ?', (user_id,))
                cursor.execute('DELETE FROM users WHERE id = ?', (user_id,))
                cursor.execute('COMMIT')

                logger.info(f"用户及相关数据删除成功: user_id={user_id}")
                return True

            except Exception as e:
                logger.error(f"删除用户及相关数据失败: {e}")
                try:
                    if cursor is not None:
                        cursor.execute('ROLLBACK')
                    else:
                        self.conn.rollback()
                except Exception:
                    try:
                        self.conn.rollback()
                    except Exception:
                        pass
                raise

    def get_table_data(self, table_name: str):
        """获取指定表的所有数据"""
        with self.lock:
            try:
                cursor = self.conn.cursor()

                # 获取表结构
                cursor.execute(f"PRAGMA table_info({table_name})")
                columns_info = cursor.fetchall()
                columns = [col[1] for col in columns_info]  # 列名

                # 获取表数据，并补充 rowid 供管理员数据管理模块做稳定删除
                cursor.execute(f"SELECT rowid, * FROM {table_name}")
                rows = cursor.fetchall()

                # 转换为字典列表
                data = []
                for row in rows:
                    row_dict = {"__admin_rowid": row[0]}
                    for i, value in enumerate(row[1:]):
                        row_dict[columns[i]] = value
                    data.append(row_dict)

                if table_name == "system_settings":
                    data = [
                        row_dict
                        for row_dict in data
                        if str(row_dict.get("key") or "").strip()
                        not in self.ADMIN_DATA_HIDDEN_SYSTEM_SETTING_KEYS
                    ]

                return data, columns

            except Exception as e:
                logger.error(f"获取表数据失败: {table_name} - {e}")
                raise

    def _delete_account_scoped_rows(self, cursor, account_ids: List[str]) -> None:
        normalized_account_ids = [str(account_id or '').strip() for account_id in account_ids if str(account_id or '').strip()]
        if not normalized_account_ids:
            return

        placeholders = ",".join(["?"] * len(normalized_account_ids))
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

        # SQLite 默认不启用外键级联，这里手动兜底清理账号作用域数据。
        for table_name in account_scoped_tables:
            cursor.execute(
                f"DELETE FROM {table_name} WHERE account_id IN ({placeholders})",
                normalized_account_ids,
            )

    def _delete_user_scoped_rows(self, cursor, user_id: int) -> None:
        user_scoped_tables = (
            "user_settings",
            "cards",
            "delivery_rules",
            "notification_channels",
            "notification_templates",
        )
        for table_name in user_scoped_tables:
            cursor.execute(f"DELETE FROM {table_name} WHERE user_id = ?", (user_id,))

        cursor.execute("DELETE FROM delivery_logs WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM scheduled_tasks WHERE user_id = ?", (user_id,))

    # 已知的无效 buyer_id 占位值
    _INVALID_BUYER_IDS = {"unknown_user", "unknown", "", "None", "null", "0", "-", "-1"}

    @staticmethod
    def _is_valid_buyer_id(buyer_id) -> bool:
        """检查 buyer_id 是否为有效值（非占位符）"""
        if not buyer_id:
            return False
        normalized_buyer_id = str(buyer_id).strip()
        if normalized_buyer_id.endswith('@goofish'):
            normalized_buyer_id = normalized_buyer_id.split('@')[0].strip()
        if normalized_buyer_id in DBManager._INVALID_BUYER_IDS:
            return False
        if normalized_buyer_id.isdigit() and len(normalized_buyer_id) <= 2:
            return False
        return True














    def delete_table_record(self, table_name: str, record_id: str):
        """删除指定表的指定记录"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute('BEGIN TRANSACTION')

                if table_name == "users":
                    cursor.execute("SELECT id FROM users WHERE rowid = ?", (record_id,))
                    user_row = cursor.fetchone()
                    if not user_row or user_row[0] is None:
                        self.conn.rollback()
                        logger.warning(f"删除表记录失败，用户记录不存在: {table_name}.{record_id}")
                        return False

                    target_user_id = int(user_row[0])
                    cursor.execute("SELECT id FROM cookies WHERE user_id = ?", (target_user_id,))
                    user_account_ids = [
                        str(row[0]).strip()
                        for row in cursor.fetchall()
                        if str(row[0] or '').strip()
                    ]
                    self._delete_user_scoped_rows(cursor, target_user_id)
                    self._delete_account_scoped_rows(cursor, user_account_ids)
                    cursor.execute("DELETE FROM cookies WHERE user_id = ?", (target_user_id,))
                    cursor.execute("DELETE FROM users WHERE id = ?", (target_user_id,))
                elif table_name == "cookies":
                    cursor.execute("SELECT id FROM cookies WHERE rowid = ?", (record_id,))
                    cookie_row = cursor.fetchone()
                    if not cookie_row or cookie_row[0] is None:
                        self.conn.rollback()
                        logger.warning(f"删除表记录失败，账号记录不存在: {table_name}.{record_id}")
                        return False

                    normalized_account_id = self._require_account_id(cookie_row[0])
                    self._delete_account_scoped_rows(cursor, [normalized_account_id])
                    cursor.execute("DELETE FROM cookies WHERE id = ?", (normalized_account_id,))
                elif table_name == "system_settings":
                    cursor.execute("SELECT key FROM system_settings WHERE rowid = ?", (record_id,))
                    setting_row = cursor.fetchone()
                    if not setting_row or setting_row[0] is None:
                        self.conn.rollback()
                        logger.warning(f"删除表记录失败，系统设置记录不存在: {table_name}.{record_id}")
                        return False

                    setting_key = str(setting_row[0]).strip()
                    if setting_key == "admin_password_hash":
                        self.conn.rollback()
                        logger.warning("拒绝删除 system_settings.admin_password_hash 记录")
                        return False

                    cursor.execute("DELETE FROM system_settings WHERE rowid = ?", (record_id,))
                else:
                    # 管理后台数据管理统一使用 rowid 删除，避免文本主键/组合主键表删错或删不掉
                    cursor.execute(f"DELETE FROM {table_name} WHERE rowid = ?", (record_id,))

                if cursor.rowcount > 0:
                    self.conn.commit()
                    logger.info(f"删除表记录成功: {table_name}.{record_id}")
                    return True
                else:
                    logger.warning(f"删除表记录失败，记录不存在: {table_name}.{record_id}")
                    return False

            except Exception as e:
                logger.error(f"删除表记录失败: {table_name}.{record_id} - {e}")
                self.conn.rollback()
                raise

    def clear_table_data(self, table_name: str):
        """清空指定表的所有数据"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute('BEGIN TRANSACTION')

                if table_name == "users":
                    self.conn.rollback()
                    logger.warning("拒绝直接清空 users 表，避免破坏管理员账号")
                    return False

                if table_name == "cookies":
                    cursor.execute("SELECT id FROM cookies")
                    account_ids = [
                        str(row[0]).strip()
                        for row in cursor.fetchall()
                        if str(row[0] or '').strip()
                    ]
                    self._delete_account_scoped_rows(cursor, account_ids)
                    cursor.execute("DELETE FROM cookies")
                elif table_name == "system_settings":
                    cursor.execute("DELETE FROM system_settings WHERE key != 'admin_password_hash'")
                else:
                    # 清空表数据
                    cursor.execute(f"DELETE FROM {table_name}")

                # 重置自增ID（如果有的话）
                cursor.execute(f"DELETE FROM sqlite_sequence WHERE name = ?", (table_name,))

                self.conn.commit()
                logger.info(f"清空表数据成功: {table_name}")
                return True

            except Exception as e:
                logger.error(f"清空表数据失败: {table_name} - {e}")
                self.conn.rollback()
                raise

    def upgrade_keywords_table_for_image_support(self, cursor):
        """升级keywords表以支持图片关键词"""
        try:
            logger.info("开始升级keywords表以支持图片关键词...")

            # 检查是否已经有type字段
            cursor.execute("PRAGMA table_info(keywords)")
            columns = [column[1] for column in cursor.fetchall()]

            if 'type' not in columns:
                logger.info("添加type字段到keywords表...")
                cursor.execute("ALTER TABLE keywords ADD COLUMN type TEXT DEFAULT 'text'")

            if 'image_url' not in columns:
                logger.info("添加image_url字段到keywords表...")
                cursor.execute("ALTER TABLE keywords ADD COLUMN image_url TEXT")

            # 为现有记录设置默认类型
            cursor.execute("UPDATE keywords SET type = 'text' WHERE type IS NULL")

            logger.info("keywords表升级完成")
            return True

        except Exception as e:
            logger.error(f"升级keywords表失败: {e}")
            raise






    # ==================== 风控日志管理 ====================

    def _serialize_risk_control_event_meta(self, event_meta: Any) -> Optional[str]:
        if event_meta is None:
            return None
        if isinstance(event_meta, str):
            text = event_meta.strip()
            return text or None
        try:
            return json.dumps(event_meta, ensure_ascii=False, sort_keys=True)
        except Exception as e:
            logger.warning(f"序列化风控日志event_meta失败: {e}")
            return None

    def _decode_risk_control_event_meta(self, event_meta: Any) -> Optional[Any]:
        if event_meta is None:
            return None
        if isinstance(event_meta, (dict, list)):
            return event_meta
        if not isinstance(event_meta, str):
            return None
        text = event_meta.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return text

    def _extract_legacy_risk_duration_ms(self, *values: Any) -> Optional[int]:
        duration_pattern = re.compile(r'耗时[:：]\s*([0-9]+(?:\.[0-9]+)?)\s*秒')
        for value in values:
            text = str(value or '').strip()
            if not text:
                continue
            match = duration_pattern.search(text)
            if not match:
                continue
            try:
                return max(0, int(float(match.group(1)) * 1000))
            except Exception:
                continue
        return None

    def _extract_legacy_verification_url(self, *values: Any) -> Optional[str]:
        url_pattern = re.compile(r'https?://\S+')
        for value in values:
            text = str(value or '').strip()
            if not text:
                continue
            match = url_pattern.search(text)
            if match:
                return match.group(0).rstrip('),，。；;')
        return None

    def _build_legacy_verification_meta(self, verification_url: str = None) -> Optional[Dict[str, Any]]:
        text = str(verification_url or '').strip()
        if not text:
            return None

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
        except Exception:
            return {'verification_source': text[:120]}

    def _infer_legacy_risk_trigger_scene(self, log_info: Dict[str, Any]) -> Optional[str]:
        existing = str(log_info.get('trigger_scene') or '').strip()
        if existing:
            return existing

        event_type = str(log_info.get('event_type') or '').strip()
        description = str(log_info.get('event_description') or '').strip()
        processing_result = str(log_info.get('processing_result') or '').strip()
        error_message = str(log_info.get('error_message') or '').strip()
        combined_text = ' '.join(part for part in (description, processing_result, error_message) if part)
        lower_text = combined_text.lower()

        if '手动触发账密cookie刷新' in description or '账密登录方式' in description:
            return 'manual_password_refresh'
        if '手动触发扫码cookie刷新' in description:
            return 'manual_qr_refresh'
        if '扫码登录获取真实cookie' in description:
            return 'qr_login'

        if event_type in {'face_verify', 'sms_verify', 'qr_verify', 'unknown', 'password_error'}:
            return 'password_login'

        if '连续失败5次' in description or '关键api不可用' in lower_text or 'cookie验证失败' in description:
            return 'auto_cookie_refresh'

        if 'token刷新' in combined_text or '令牌' in combined_text or 'session过期' in lower_text or 'token' in lower_text:
            return 'token_refresh'

        if event_type == 'cookie_refresh':
            return 'auto_cookie_refresh'

        return None

    def _get_risk_trigger_scene_label(self, trigger_scene: Optional[str]) -> Optional[str]:
        scene = str(trigger_scene or '').strip()
        if not scene:
            return None
        scene_labels = {
            'token_refresh': 'Token刷新',
            'auto_cookie_refresh': '自动Cookie刷新',
            'manual_password_refresh': '手动账密刷新',
            'manual_qr_refresh': '手动扫码刷新',
            'password_login': '密码登录',
            'qr_login': '扫码登录',
        }
        return scene_labels.get(scene, scene)

    def _compact_legacy_risk_description(self, log_info: Dict[str, Any]) -> str:
        description = str(log_info.get('event_description') or '').strip()
        if not description:
            return ''

        event_type = str(log_info.get('event_type') or '').strip()
        trigger_scene = self._get_risk_trigger_scene_label(log_info.get('trigger_scene'))
        lower_description = description.lower()

        if event_type == 'slider_captcha' and ('滑块验证' in description or 'url:' in lower_description):
            return f"检测到滑块验证（{trigger_scene}）" if trigger_scene else '检测到滑块验证'

        if event_type == 'token_expired':
            if 'session过期' in lower_description:
                return '检测到Session过期'
            if '令牌过期' in description:
                return '检测到令牌过期'
            return '检测到令牌/Session过期'

        if event_type == 'cookie_refresh':
            replacements = {
                '手动触发Cookie刷新（账密登录方式）': '手动触发账密Cookie刷新',
                '手动触发Cookie刷新（扫码登录方式）': '手动触发扫码Cookie刷新',
                '令牌/Session过期触发Cookie刷新和实例重启': '令牌/Session过期触发Cookie刷新',
                '连续失败5次触发Cookie刷新和实例重启': '连续失败5次触发Cookie刷新',
                'Cookie验证失败(关键API不可用)触发Cookie刷新和实例重启': 'Cookie验证失败（关键API不可用）触发Cookie刷新',
                '滑块成功后Token预热失败触发Cookie刷新和实例重启': '滑块成功后Token预热失败，触发Cookie刷新',
            }
            if description in replacements:
                return replacements[description]

        compacted = re.sub(r'[，,]?\s*URL[:：]\s*https?://\S+', '', description, flags=re.IGNORECASE)
        compacted = re.sub(r'https?://\S+', '', compacted)
        compacted = compacted.replace('准备刷新Cookie并重启实例', '准备刷新Cookie')
        compacted = compacted.replace('触发Cookie刷新和实例重启', '触发Cookie刷新')
        compacted = compacted.replace('  ', ' ')
        compacted = compacted.strip(' ，,;；')
        return compacted or description

    def _compact_legacy_risk_processing_result(self, log_info: Dict[str, Any]) -> str:
        processing_result = str(log_info.get('processing_result') or '').strip()
        if not processing_result:
            return ''

        event_type = str(log_info.get('event_type') or '').strip()
        error_message = str(log_info.get('error_message') or '').strip()
        lower_result = processing_result.lower()

        if event_type == 'slider_captcha':
            if '滑块验证成功' in processing_result:
                return '滑块验证成功，已获取新Cookie'

            reason_match = re.search(r'原因[:：]\s*(.+)$', processing_result)
            if reason_match:
                reason = reason_match.group(1).strip(' ，,;；')
                if '未获取到新cookies' in reason or '未获取到新cookie' in reason.lower():
                    reason = '未获取到新Cookie'
                elif '触发闲鱼风控验证' in reason:
                    reason = '触发闲鱼风控验证'
                return f'滑块验证失败（{reason}）'

            if '触发闲鱼风控验证' in processing_result or '触发闲鱼风控验证' in error_message:
                return '滑块验证失败（触发闲鱼风控验证）'

        if event_type == 'cookie_refresh':
            if '扫码登录真实Cookie获取成功，账号任务已启动' in processing_result:
                if 'Token预热未完成' in processing_result:
                    return '真实Cookie获取成功，Token预热待重试'
                return '真实Cookie获取成功，账号任务已启动'

            cookie_refresh_result_map = {
                'Cookie刷新成功': 'Cookie刷新成功',
                '扫码登录真实Cookie获取成功，但未切换到新任务': '真实Cookie获取成功，但未切换到新任务',
                '密码登录刷新Cookie成功，实例已重启': '密码登录刷新Cookie成功，实例已重启',
            }
            if processing_result in cookie_refresh_result_map:
                return cookie_refresh_result_map[processing_result]

        compacted = re.sub(r'[，,]\s*耗时[:：]\s*[0-9]+(?:\.[0-9]+)?\s*秒', '', processing_result)
        compacted = re.sub(r'[，,]\s*cookies?长度[:：]?\s*\d+', '', compacted, flags=re.IGNORECASE)
        compacted = compacted.replace('未获取到新cookies', '未获取到新Cookie')
        compacted = compacted.replace('未获取到新cookie', '未获取到新Cookie')
        compacted = compacted.replace('  ', ' ')
        compacted = compacted.strip(' ，,;；')
        return compacted or processing_result

    def _compact_legacy_risk_error_message(self, log_info: Dict[str, Any]) -> str:
        error_message = str(log_info.get('error_message') or '').strip()
        if not error_message:
            return ''

        compact_mappings = {
            "cannot access local variable 'is_refresh_mode' where it is not associated with a value": '账密刷新流程变量异常',
            '真实Cookie已获取，但首次Token初始化失败，未切换到新的账号任务': '真实Cookie已获取，但首次Token初始化失败',
            '当前登录页被风控拦截，出现前置滑块，请稍后重试': '当前登录页被风控拦截',
        }
        if error_message in compact_mappings:
            return compact_mappings[error_message]

        if 'No space left on device' in error_message:
            return '磁盘空间不足'

        if '触发闲鱼风控验证' in error_message:
            return '触发闲鱼风控验证'

        if error_message.startswith('触发场景:') and 'URL:' in error_message:
            if '密码登录' in error_message:
                return '密码登录触发验证'
            if '扫码登录' in error_message:
                return '扫码登录触发验证'
            return '触发身份验证'

        if error_message.startswith('滑块验证失败：'):
            reason = error_message.split('：', 1)[1].strip()
            return f'滑块验证失败（{reason}）' if reason else '滑块验证失败'

        compacted = re.sub(r'[，,]?\s*URL[:：]\s*https?://\S+', '', error_message, flags=re.IGNORECASE)
        compacted = re.sub(r'https?://\S+', '', compacted)
        compacted = compacted.replace('  ', ' ')
        compacted = compacted.strip(' ，,;；')
        return compacted or error_message

    def _normalize_legacy_risk_log(self, log_info: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(log_info)
        session_id = str(normalized.get('session_id') or '').strip()
        trigger_scene = str(normalized.get('trigger_scene') or '').strip()
        result_code = str(normalized.get('result_code') or '').strip()
        raw_meta = normalized.get('event_meta')
        duration_ms = normalized.get('duration_ms')

        is_legacy = not any([session_id, trigger_scene, result_code, raw_meta, duration_ms])

        inferred_trigger_scene = self._infer_legacy_risk_trigger_scene(normalized)
        if inferred_trigger_scene and not trigger_scene:
            normalized['trigger_scene'] = inferred_trigger_scene

        if duration_ms in (None, ''):
            inferred_duration_ms = self._extract_legacy_risk_duration_ms(
                normalized.get('processing_result'),
                normalized.get('error_message'),
                normalized.get('event_description'),
            )
            if inferred_duration_ms is not None:
                normalized['duration_ms'] = inferred_duration_ms

        if not raw_meta:
            verification_url = self._extract_legacy_verification_url(
                normalized.get('event_description'),
                normalized.get('error_message'),
            )
            legacy_meta = self._build_legacy_verification_meta(verification_url)
            if legacy_meta:
                legacy_meta['legacy_record'] = True
                if normalized.get('trigger_scene'):
                    legacy_meta['trigger_scene'] = normalized.get('trigger_scene')
                normalized['event_meta'] = legacy_meta
        elif isinstance(raw_meta, dict) and is_legacy:
            legacy_meta = dict(raw_meta)
            legacy_meta.setdefault('legacy_record', True)
            if normalized.get('trigger_scene'):
                legacy_meta.setdefault('trigger_scene', normalized.get('trigger_scene'))
            normalized['event_meta'] = legacy_meta

        normalized['event_description_display'] = self._compact_legacy_risk_description(normalized) or normalized.get('event_description') or '-'
        if is_legacy:
            normalized['processing_result_display'] = self._compact_legacy_risk_processing_result(normalized) or normalized.get('processing_result') or ''
            normalized['error_message_display'] = self._compact_legacy_risk_error_message(normalized) or normalized.get('error_message') or ''
        else:
            normalized['processing_result_display'] = normalized.get('processing_result') or ''
            normalized['error_message_display'] = normalized.get('error_message') or ''
        normalized['is_legacy'] = is_legacy
        normalized['session_display'] = session_id or ('历史记录' if is_legacy else '--')
        return normalized

    def _normalize_risk_log_datetime_param(self, value: Any, end_of_day: bool = False) -> Optional[str]:
        text = str(value or '').strip()
        if not text:
            return None
        if len(text) == 10 and text.count('-') == 2:
            suffix = '23:59:59' if end_of_day else '00:00:00'
            return f"{text} {suffix}"
        return text[:19]

    def _build_risk_control_log_filters(
        self,
        alias: str = '',
        account_id: str = None,
        processing_status: str = None,
        event_type: str = None,
        trigger_scene: str = None,
        session_id: str = None,
        result_code: str = None,
        date_from: str = None,
        date_to: str = None,
    ) -> Tuple[List[str], List[Any]]:
        prefix = ''
        if alias:
            prefix = alias if alias.endswith('.') else f"{alias}."

        conditions: List[str] = []
        params: List[Any] = []
        normalized_account_id = (
            self._require_account_id(account_id) if account_id is not None else None
        )

        filter_specs = [
            ('account_id', normalized_account_id),
            ('processing_status', processing_status),
            ('event_type', event_type),
            ('trigger_scene', trigger_scene),
            ('session_id', session_id),
            ('result_code', result_code),
        ]
        for column_name, raw_value in filter_specs:
            value = str(raw_value or '').strip()
            if not value:
                continue
            conditions.append(f"{prefix}{column_name} = ?")
            params.append(value)

        normalized_from = self._normalize_risk_log_datetime_param(date_from, end_of_day=False)
        if normalized_from:
            conditions.append(f"datetime({prefix}created_at) >= datetime(?)")
            params.append(normalized_from)

        normalized_to = self._normalize_risk_log_datetime_param(date_to, end_of_day=True)
        if normalized_to:
            conditions.append(f"datetime({prefix}created_at) <= datetime(?)")
            params.append(normalized_to)

        return conditions, params


    def update_risk_control_log(self, log_id: int, event_description: str = None,
                              processing_result: str = None, processing_status: str = None,
                              error_message: str = None, session_id: str = None,
                              trigger_scene: str = None, result_code: str = None,
                              event_meta: Any = None, duration_ms: Optional[int] = None) -> bool:
        """
        更新风控日志记录

        Args:
            log_id: 日志ID
            event_description: 事件描述
            processing_result: 处理结果
            processing_status: 处理状态
            error_message: 错误信息
            session_id: 事件链路ID
            trigger_scene: 触发场景
            result_code: 结果代码
            event_meta: 结构化扩展信息
            duration_ms: 处理耗时（毫秒）

        Returns:
            bool: 更新成功返回True，失败返回False
        """
        try:
            with self.lock:
                cursor = self.conn.cursor()

                # 构建更新语句
                update_fields = []
                params = []

                if event_description is not None:
                    update_fields.append("event_description = ?")
                    params.append(event_description)

                if processing_result is not None:
                    update_fields.append("processing_result = ?")
                    params.append(processing_result)

                if processing_status is not None:
                    update_fields.append("processing_status = ?")
                    params.append(processing_status)

                if error_message is not None:
                    update_fields.append("error_message = ?")
                    params.append(error_message)

                if session_id is not None:
                    update_fields.append("session_id = ?")
                    params.append(session_id)

                if trigger_scene is not None:
                    update_fields.append("trigger_scene = ?")
                    params.append(trigger_scene)

                if result_code is not None:
                    update_fields.append("result_code = ?")
                    params.append(result_code)

                if event_meta is not None:
                    update_fields.append("event_meta = ?")
                    params.append(self._serialize_risk_control_event_meta(event_meta))

                if duration_ms is not None:
                    update_fields.append("duration_ms = ?")
                    params.append(int(duration_ms))

                if update_fields:
                    update_fields.append("updated_at = CURRENT_TIMESTAMP")
                    params.append(log_id)

                    sql = f"UPDATE risk_control_logs SET {', '.join(update_fields)} WHERE id = ?"
                    cursor.execute(sql, params)
                    self.conn.commit()
                    return cursor.rowcount > 0

                return False
        except Exception as e:
            logger.error(f"更新风控日志失败: {e}")
            return False




    def delete_risk_control_log(self, log_id: int) -> bool:
        """
        删除风控日志记录

        Args:
            log_id: 日志ID

        Returns:
            bool: 删除成功返回True，失败返回False
        """
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute('DELETE FROM risk_control_logs WHERE id = ?', (log_id,))
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"删除风控日志失败: {e}")
                self.conn.rollback()
                raise

    
    def cleanup_old_data(self, days: int = 90) -> dict:
        """清理过期的历史数据，防止数据库无限增长
        
        Args:
            days: 保留最近N天的数据，默认90天
            
        Returns:
            清理统计信息
        """
        try:
            with self.lock:
                cursor = self.conn.cursor()
                stats = {}
                
                # 清理AI对话历史（保留最近90天）
                try:
                    cursor.execute(
                        "DELETE FROM ai_conversations WHERE created_at < datetime('now', '-' || ? || ' days')",
                        (days,)
                    )
                    stats['ai_conversations'] = cursor.rowcount
                    if cursor.rowcount > 0:
                        logger.info(f"清理了 {cursor.rowcount} 条过期的AI对话记录（{days}天前）")
                except Exception as e:
                    logger.warning(f"清理AI对话历史失败: {e}")
                    stats['ai_conversations'] = 0
                
                # 清理风控日志（保留最近90天）
                try:
                    cursor.execute(
                        "DELETE FROM risk_control_logs WHERE created_at < datetime('now', '-' || ? || ' days')",
                        (days,)
                    )
                    stats['risk_control_logs'] = cursor.rowcount
                    if cursor.rowcount > 0:
                        logger.info(f"清理了 {cursor.rowcount} 条过期的风控日志（{days}天前）")
                except Exception as e:
                    logger.warning(f"清理风控日志失败: {e}")
                    stats['risk_control_logs'] = 0
                
                # 清理AI商品缓存（保留最近30天）
                cache_days = min(days, 30)  # AI商品缓存最多保留30天
                try:
                    cursor.execute(
                        "DELETE FROM ai_item_cache WHERE last_updated < datetime('now', '-' || ? || ' days')",
                        (cache_days,)
                    )
                    stats['ai_item_cache'] = cursor.rowcount
                    if cursor.rowcount > 0:
                        logger.info(f"清理了 {cursor.rowcount} 条过期的AI商品缓存（{cache_days}天前）")
                except Exception as e:
                    logger.warning(f"清理AI商品缓存失败: {e}")
                    stats['ai_item_cache'] = 0
                
                # 清理验证码记录（保留最近1天）
                try:
                    cursor.execute(
                        "DELETE FROM captcha_codes WHERE created_at < datetime('now', '-1 day')"
                    )
                    stats['captcha_codes'] = cursor.rowcount
                    if cursor.rowcount > 0:
                        logger.info(f"清理了 {cursor.rowcount} 条过期的验证码记录")
                except Exception as e:
                    logger.warning(f"清理验证码记录失败: {e}")
                    stats['captcha_codes'] = 0
                
                # 清理邮箱验证记录（保留最近7天）
                try:
                    cursor.execute(
                        "DELETE FROM email_verifications WHERE created_at < datetime('now', '-7 days')"
                    )
                    stats['email_verifications'] = cursor.rowcount
                    if cursor.rowcount > 0:
                        logger.info(f"清理了 {cursor.rowcount} 条过期的邮箱验证记录")
                except Exception as e:
                    logger.warning(f"清理邮箱验证记录失败: {e}")
                    stats['email_verifications'] = 0
                
                # 提交更改
                self.conn.commit()
                
                # 执行VACUUM以释放磁盘空间（仅当清理了大量数据时）
                total_cleaned = sum(stats.values())
                if total_cleaned > 100:
                    logger.info(f"共清理了 {total_cleaned} 条记录，执行VACUUM以释放磁盘空间...")
                    cursor.execute("VACUUM")
                    logger.info("VACUUM执行完成")
                    stats['vacuum_executed'] = True
                else:
                    stats['vacuum_executed'] = False
                
                stats['total_cleaned'] = total_cleaned
                return stats
                
        except Exception as e:
            logger.error(f"清理历史数据时出错: {e}")
            return {'error': str(e)}

    # ==================== 定时任务管理 ====================

    def calculate_next_daily_run(self, run_hour, random_delay_max=10, include_today=True):
        """计算每日定时任务的下次运行时间"""
        from datetime import datetime, timedelta
        import random

        now = datetime.now()
        safe_hour = max(0, min(23, int(run_hour)))
        safe_random_max = max(0, int(random_delay_max or 0))
        random_min = random.randint(0, safe_random_max) if safe_random_max > 0 else 0

        next_run = now.replace(hour=safe_hour, minute=random_min, second=0, microsecond=0)
        if not include_today or next_run <= now:
            next_run += timedelta(days=1)

        return next_run.strftime('%Y-%m-%d %H:%M:%S')

    def create_scheduled_task(self, name, task_type, account_id, user_id=None,
                              interval_hours=24, delay_minutes=0, random_delay_max=10,
                              next_run_at=None, enabled=1):
        """创建定时任务

        Args:
            delay_minutes: 用作每日运行的目标小时 (0-23)
        """
        with self.lock:
            try:
                cursor = self.conn.cursor()
                next_run_value = next_run_at or self.calculate_next_daily_run(
                    delay_minutes,
                    random_delay_max,
                    include_today=True
                )

                self._execute_sql(cursor, """
                    INSERT INTO scheduled_tasks (name, task_type, account_id, user_id,
                        enabled, interval_hours, delay_minutes, random_delay_max, next_run_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (name, task_type, account_id, user_id,
                      1 if enabled else 0, interval_hours, delay_minutes, random_delay_max,
                      next_run_value))
                self.conn.commit()
                task_id = cursor.lastrowid
                logger.info(f"创建定时任务成功: {name} (ID: {task_id})")
                return task_id
            except Exception as e:
                logger.error(f"创建定时任务失败: {e}")
                self.conn.rollback()
                raise

    def get_scheduled_tasks(self, user_id=None):
        """获取定时任务列表"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    self._execute_sql(cursor, """
                        SELECT id, name, task_type, account_id, enabled, interval_hours,
                               delay_minutes, random_delay_max, next_run_at, last_run_at,
                               last_run_result, user_id, created_at, updated_at
                        FROM scheduled_tasks WHERE user_id = ?
                        ORDER BY id DESC
                    """, (user_id,))
                else:
                    self._execute_sql(cursor, """
                        SELECT id, name, task_type, account_id, enabled, interval_hours,
                               delay_minutes, random_delay_max, next_run_at, last_run_at,
                               last_run_result, user_id, created_at, updated_at
                        FROM scheduled_tasks ORDER BY id DESC
                    """)
                rows = cursor.fetchall()
                tasks = []
                for row in rows:
                    tasks.append({
                        'id': row[0], 'name': row[1], 'task_type': row[2],
                        'account_id': row[3], 'enabled': bool(row[4]),
                        'interval_hours': row[5], 'delay_minutes': row[6],
                        'random_delay_max': row[7], 'next_run_at': row[8],
                        'last_run_at': row[9], 'last_run_result': row[10],
                        'user_id': row[11], 'created_at': row[12], 'updated_at': row[13]
                    })
                return tasks
            except Exception as e:
                logger.error(f"获取定时任务列表失败: {e}")
                raise

    def get_scheduled_task(self, task_id):
        """获取单个定时任务"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(cursor, """
                    SELECT id, name, task_type, account_id, enabled, interval_hours,
                           delay_minutes, random_delay_max, next_run_at, last_run_at,
                           last_run_result, user_id, created_at, updated_at
                    FROM scheduled_tasks WHERE id = ?
                """, (task_id,))
                row = cursor.fetchone()
                if row:
                    return {
                        'id': row[0], 'name': row[1], 'task_type': row[2],
                        'account_id': row[3], 'enabled': bool(row[4]),
                        'interval_hours': row[5], 'delay_minutes': row[6],
                        'random_delay_max': row[7], 'next_run_at': row[8],
                        'last_run_at': row[9], 'last_run_result': row[10],
                        'user_id': row[11], 'created_at': row[12], 'updated_at': row[13]
                    }
                return None
            except Exception as e:
                logger.error(f"获取定时任务失败: {e}")
                raise

    def get_scheduled_task_by_account(self, account_id, user_id=None, task_type=None):
        """按账号获取最新的定时任务"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                params = [account_id]
                sql = """
                    SELECT id, name, task_type, account_id, enabled, interval_hours,
                           delay_minutes, random_delay_max, next_run_at, last_run_at,
                           last_run_result, user_id, created_at, updated_at
                    FROM scheduled_tasks
                    WHERE account_id = ?
                """

                if user_id is not None:
                    sql += " AND user_id = ?"
                    params.append(user_id)

                if task_type is not None:
                    sql += " AND task_type = ?"
                    params.append(task_type)

                sql += " ORDER BY enabled DESC, id DESC LIMIT 1"
                self._execute_sql(cursor, sql, tuple(params))
                row = cursor.fetchone()
                if row:
                    return {
                        'id': row[0], 'name': row[1], 'task_type': row[2],
                        'account_id': row[3], 'enabled': bool(row[4]),
                        'interval_hours': row[5], 'delay_minutes': row[6],
                        'random_delay_max': row[7], 'next_run_at': row[8],
                        'last_run_at': row[9], 'last_run_result': row[10],
                        'user_id': row[11], 'created_at': row[12], 'updated_at': row[13]
                    }
                return None
            except Exception as e:
                logger.error(f"按账号获取定时任务失败: {e}")
                raise

    def update_scheduled_task(self, task_id, **kwargs):
        """更新定时任务"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                allowed_fields = {'name', 'task_type', 'account_id', 'enabled',
                                  'interval_hours', 'delay_minutes', 'random_delay_max',
                                  'next_run_at', 'user_id'}
                update_fields = []
                params = []
                for key, value in kwargs.items():
                    if key in allowed_fields:
                        update_fields.append(f"{key} = ?")
                        params.append(value)

                if not update_fields:
                    return False

                update_fields.append("updated_at = CURRENT_TIMESTAMP")
                params.append(task_id)
                sql = f"UPDATE scheduled_tasks SET {', '.join(update_fields)} WHERE id = ?"
                self._execute_sql(cursor, sql, tuple(params))
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"更新定时任务失败: {e}")
                self.conn.rollback()
                raise

    def delete_scheduled_task(self, task_id):
        """删除定时任务"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(cursor, "DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"删除定时任务失败: {e}")
                self.conn.rollback()
                raise

    def get_due_tasks(self):
        """获取到期需要执行的任务"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                from datetime import datetime
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self._execute_sql(cursor, """
                    SELECT id, name, task_type, account_id, enabled, interval_hours,
                           delay_minutes, random_delay_max, next_run_at, last_run_at,
                           last_run_result, user_id, created_at, updated_at
                    FROM scheduled_tasks
                    WHERE enabled = 1 AND next_run_at <= ?
                    ORDER BY next_run_at ASC
                """, (now,))
                rows = cursor.fetchall()
                tasks = []
                for row in rows:
                    tasks.append({
                        'id': row[0], 'name': row[1], 'task_type': row[2],
                        'account_id': row[3], 'enabled': bool(row[4]),
                        'interval_hours': row[5], 'delay_minutes': row[6],
                        'random_delay_max': row[7], 'next_run_at': row[8],
                        'last_run_at': row[9], 'last_run_result': row[10],
                        'user_id': row[11], 'created_at': row[12], 'updated_at': row[13]
                    })
                return tasks
            except Exception as e:
                logger.error(f"获取到期任务失败: {e}")
                raise

    def update_task_run_result(self, task_id, result, next_run_at):
        """更新任务执行结果和下次运行时间"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                from datetime import datetime
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                result_str = json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else str(result)
                self._execute_sql(cursor, """
                    UPDATE scheduled_tasks
                    SET last_run_at = ?, last_run_result = ?, next_run_at = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (now, result_str, next_run_at, task_id))
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"更新任务执行结果失败: {e}")
                self.conn.rollback()
                return False


# 全局单例
    def _require_account_id(self, account_id: str) -> str:
        normalized_account_id = str(account_id or "").strip()
        if not normalized_account_id:
            raise ValueError("account_id 不能为空")
        if normalized_account_id == "default":
            raise ValueError("account_id 不能为 default")
        if not ACCOUNT_ID_PATTERN.fullmatch(normalized_account_id):
            raise ValueError("account_id 只能包含英文字母、数字、下划线和短横线")
        return normalized_account_id

    def _expand_order_status_filter(self, status: str) -> List[str]:
        normalized_status = self._normalize_order_status(status) or str(status or "").strip()
        if not normalized_status:
            return []
        if normalized_status == "pending_ship":
            return [
                "pending_ship",
                "pending_delivery",
                "partial_success",
                "partial_pending_finalize",
            ]
        return [normalized_status]

    def _order_row_to_dict(self, row: Tuple[Any, ...]) -> Dict[str, Any]:
        return {
            "order_id": row[0],
            "item_id": row[1],
            "buyer_id": row[2],
            "buyer_nick": row[3],
            "sid": row[4],
            "spec_name": row[5],
            "spec_value": row[6],
            "spec_name_2": row[7],
            "spec_value_2": row[8],
            "quantity": row[9],
            "amount": row[10],
            "bargain_flow_detected": bool(row[11]),
            "bargain_success_detected": bool(row[12]),
            "order_status": row[13],
            "pre_refund_status": row[14],
            "account_id": row[15],
            "platform_created_at": row[16],
            "platform_paid_at": row[17],
            "platform_completed_at": row[18],
            "created_at": row[19],
            "updated_at": row[20],
            "yifan_orderno": row[21],
            "delivery_status": row[22],
            "callback_data": row[23],
            "chat_id": row[24],
        }

    def insert_or_update_order(self, order_id: str, item_id: str = None, buyer_id: str = None,
                               spec_name: str = None, spec_value: str = None, quantity: str = None,
                               amount: str = None, order_status: str = None, *, account_id: str,
                               sid: str = None, spec_name_2: str = None, spec_value_2: str = None,
                               buyer_nick: str = None, pre_refund_status=..., clear_pre_refund_status: bool = False,
                               bargain_flow_detected=..., bargain_success_detected=...,
                               platform_created_at: str = None, platform_paid_at: str = None,
                               platform_completed_at: str = None) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                normalized_order_status = self._normalize_order_status(order_status)
                has_pre_refund_status = pre_refund_status is not ...
                normalized_pre_refund_status = None
                if has_pre_refund_status:
                    normalized_pre_refund_status = self._normalize_order_status(pre_refund_status)

                cursor.execute("SELECT id FROM cookies WHERE id = ?", (normalized_account_id,))
                if not cursor.fetchone():
                    logger.warning(
                        f"account_id 不存在于 cookies 表中，拒绝写入订单: account_id={normalized_account_id}, order_id={order_id}"
                    )
                    return False

                cursor.execute("SELECT account_id FROM orders WHERE order_id = ?", (order_id,))
                existing_row = cursor.fetchone()

                if existing_row:
                    existing_account_id = str(existing_row[0] or "").strip()
                    if not existing_account_id or existing_account_id != normalized_account_id:
                        logger.warning(
                            f"拒绝跨账号覆盖订单: order_id={order_id}, existing_account_id={existing_row[0]}, "
                            f"incoming_account_id={normalized_account_id}"
                        )
                        return False

                    update_fields = []
                    update_values = []

                    if item_id is not None:
                        update_fields.append("item_id = ?")
                        update_values.append(item_id)
                    if buyer_id is not None:
                        if self._is_valid_buyer_id(buyer_id):
                            update_fields.append("buyer_id = ?")
                            update_values.append(buyer_id)
                        else:
                            logger.debug(f"跳过无效 buyer_id 覆盖: order_id={order_id}, buyer_id={buyer_id}")
                    if buyer_nick is not None:
                        update_fields.append("buyer_nick = ?")
                        update_values.append(buyer_nick)
                    if sid is not None:
                        update_fields.append("sid = ?")
                        update_values.append(sid)
                    if spec_name is not None:
                        update_fields.append("spec_name = ?")
                        update_values.append(spec_name)
                    if spec_value is not None:
                        update_fields.append("spec_value = ?")
                        update_values.append(spec_value)
                    if spec_name_2 is not None:
                        update_fields.append("spec_name_2 = ?")
                        update_values.append(spec_name_2)
                    if spec_value_2 is not None:
                        update_fields.append("spec_value_2 = ?")
                        update_values.append(spec_value_2)
                    if quantity is not None:
                        update_fields.append("quantity = ?")
                        update_values.append(quantity)
                    if amount is not None:
                        update_fields.append("amount = ?")
                        update_values.append(amount)
                    if bargain_flow_detected is not ...:
                        update_fields.append("bargain_flow_detected = ?")
                        update_values.append(1 if bargain_flow_detected else 0)
                    if bargain_success_detected is not ...:
                        update_fields.append("bargain_success_detected = ?")
                        update_values.append(1 if bargain_success_detected else 0)
                    if order_status is not None:
                        update_fields.append("order_status = ?")
                        update_values.append(normalized_order_status or "unknown")
                    if clear_pre_refund_status:
                        update_fields.append("pre_refund_status = NULL")
                    elif has_pre_refund_status:
                        update_fields.append("pre_refund_status = ?")
                        update_values.append(normalized_pre_refund_status)
                    if platform_created_at is not None:
                        update_fields.append("platform_created_at = ?")
                        update_values.append(platform_created_at)
                    if platform_paid_at is not None:
                        update_fields.append("platform_paid_at = ?")
                        update_values.append(platform_paid_at)
                    if platform_completed_at is not None:
                        update_fields.append("platform_completed_at = ?")
                        update_values.append(platform_completed_at)

                    if update_fields:
                        update_fields.append("updated_at = CURRENT_TIMESTAMP")
                        update_values.extend([order_id, normalized_account_id])
                        cursor.execute(
                            f"UPDATE orders SET {', '.join(update_fields)} WHERE order_id = ? AND account_id = ?",
                            update_values,
                        )
                        if cursor.rowcount <= 0:
                            self.conn.rollback()
                            return False
                        logger.info(f"更新订单信息成功: order_id={order_id}, account_id={normalized_account_id}")
                else:
                    sanitized_buyer_id = buyer_id if self._is_valid_buyer_id(buyer_id) else None
                    insert_fields = [
                        "order_id",
                        "item_id",
                        "buyer_id",
                        "buyer_nick",
                        "sid",
                        "spec_name",
                        "spec_value",
                        "spec_name_2",
                        "spec_value_2",
                        "quantity",
                        "amount",
                        "order_status",
                        "account_id",
                    ]
                    insert_values = [
                        order_id,
                        item_id,
                        sanitized_buyer_id,
                        buyer_nick,
                        sid,
                        spec_name,
                        spec_value,
                        spec_name_2,
                        spec_value_2,
                        quantity,
                        amount,
                        normalized_order_status or "unknown",
                        normalized_account_id,
                    ]

                    if bargain_flow_detected is not ...:
                        insert_fields.append("bargain_flow_detected")
                        insert_values.append(1 if bargain_flow_detected else 0)
                    if bargain_success_detected is not ...:
                        insert_fields.append("bargain_success_detected")
                        insert_values.append(1 if bargain_success_detected else 0)
                    if platform_created_at is not None:
                        insert_fields.append("platform_created_at")
                        insert_values.append(platform_created_at)
                    if platform_paid_at is not None:
                        insert_fields.append("platform_paid_at")
                        insert_values.append(platform_paid_at)
                    if platform_completed_at is not None:
                        insert_fields.append("platform_completed_at")
                        insert_values.append(platform_completed_at)
                    if has_pre_refund_status and not clear_pre_refund_status:
                        insert_fields.append("pre_refund_status")
                        insert_values.append(normalized_pre_refund_status)

                    insert_placeholders = ", ".join(["?"] * len(insert_fields))
                    cursor.execute(
                        f"INSERT INTO orders ({', '.join(insert_fields)}) VALUES ({insert_placeholders})",
                        insert_values,
                    )
                    logger.info(f"插入新订单成功: order_id={order_id}, account_id={normalized_account_id}")

                self.conn.commit()
                return True
            except Exception as e:
                logger.error(f"插入或更新订单失败: order_id={order_id}, account_id={account_id}, error={e}")
                self.conn.rollback()
                raise

    def get_order_by_id(self, order_id: str, *, account_id: str, user_id: int = None):
        normalized_account_id = self._require_account_id(account_id)
        if user_id is not None and not self.assert_cookie_belongs_to_user(normalized_account_id, user_id):
            return None

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT order_id, item_id, buyer_id, buyer_nick, sid, spec_name, spec_value,
                           spec_name_2, spec_value_2, quantity, amount, bargain_flow_detected, bargain_success_detected,
                           order_status, pre_refund_status, account_id, platform_created_at, platform_paid_at,
                           platform_completed_at, created_at, updated_at, yifan_orderno, delivery_status,
                           callback_data, chat_id
                    FROM orders
                    WHERE order_id = ? AND account_id = ?
                    """,
                    (order_id, normalized_account_id),
                )
                row = cursor.fetchone()
                return self._order_row_to_dict(row) if row else None
            except Exception as e:
                logger.error(f"获取订单信息失败: order_id={order_id}, account_id={account_id}, error={e}")
                raise

    def get_order_pre_refund_status(self, order_id: str, *, account_id: str) -> str:
        normalized_account_id = self._require_account_id(account_id)
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "SELECT pre_refund_status FROM orders WHERE order_id = ? AND account_id = ?",
                    (order_id, normalized_account_id),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                return self._normalize_order_status(row[0]) if row[0] else None
            except Exception as e:
                logger.error(
                    f"获取订单退款前状态失败: order_id={order_id}, account_id={account_id}, error={e}"
                )
                raise

    def get_orders_by_account(self, account_id: str, limit: Optional[int] = 100):
        normalized_account_id = self._require_account_id(account_id)
        with self.lock:
            try:
                cursor = self.conn.cursor()
                sql = """
                    SELECT order_id, item_id, buyer_id, buyer_nick, sid, spec_name, spec_value,
                           spec_name_2, spec_value_2, quantity, amount, bargain_flow_detected, bargain_success_detected,
                           order_status, pre_refund_status, account_id, platform_created_at, platform_paid_at,
                           platform_completed_at, created_at, updated_at, yifan_orderno, delivery_status,
                           callback_data, chat_id
                    FROM orders
                    WHERE account_id = ?
                    ORDER BY datetime(COALESCE(updated_at, created_at)) DESC, created_at DESC
                """
                params = [normalized_account_id]
                if limit is not None:
                    sql += "\n                    LIMIT ?"
                    params.append(max(1, int(limit)))

                cursor.execute(sql, tuple(params))
                return [self._order_row_to_dict(row) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"获取账号订单列表失败: account_id={account_id}, error={e}")
                raise

    def delete_order(self, order_id: str, *, account_id: str) -> bool:
        normalized_account_id = self._require_account_id(account_id)
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "DELETE FROM orders WHERE order_id = ? AND account_id = ?",
                    (order_id, normalized_account_id),
                )
                if cursor.rowcount > 0:
                    self.conn.commit()
                    logger.info(f"删除订单成功: order_id={order_id}, account_id={normalized_account_id}")
                    return True
                self.conn.rollback()
                logger.warning(f"删除订单失败，订单不存在或账号不匹配: order_id={order_id}, account_id={normalized_account_id}")
                return False
            except Exception as e:
                logger.error(f"删除订单失败: order_id={order_id}, account_id={account_id}, error={e}")
                self.conn.rollback()
                raise

    def update_buyer_nick_by_buyer_id(self, buyer_id: str, buyer_nick: str, *, account_id: str):
        normalized_account_id = self._require_account_id(account_id)
        if not buyer_id or not buyer_nick:
            return 0

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    UPDATE orders
                    SET buyer_nick = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE buyer_id = ? AND account_id = ?
                    """,
                    (buyer_nick, buyer_id, normalized_account_id),
                )
                updated_count = cursor.rowcount
                self.conn.commit()
                if updated_count > 0:
                    logger.info(
                        f"更新买家昵称成功: buyer_id={buyer_id}, account_id={normalized_account_id}, count={updated_count}"
                    )
                return updated_count
            except Exception as e:
                logger.error(
                    f"更新买家昵称失败: buyer_id={buyer_id}, account_id={account_id}, error={e}"
                )
                self.conn.rollback()
                return 0

    def get_recent_order_by_buyer_id(self, buyer_id: str, *, account_id: str, status: str = None, minutes: int = 10):
        normalized_account_id = self._require_account_id(account_id)
        with self.lock:
            try:
                cursor = self.conn.cursor()
                conditions = ["buyer_id = ?", "account_id = ?"]
                params = [buyer_id, normalized_account_id]

                status_filters = self._expand_order_status_filter(status) if status else []
                if status_filters:
                    placeholders = ", ".join(["?"] * len(status_filters))
                    conditions.append(f"order_status IN ({placeholders})")
                    params.extend(status_filters)

                conditions.append("datetime(COALESCE(updated_at, created_at)) >= datetime('now', ?)")
                params.append(f"-{minutes} minutes")

                cursor.execute(
                    f"""
                    SELECT order_id, item_id, buyer_id, buyer_nick, sid, spec_name, spec_value,
                           spec_name_2, spec_value_2, quantity, amount, bargain_flow_detected, bargain_success_detected,
                           order_status, pre_refund_status, account_id, platform_created_at, platform_paid_at,
                           platform_completed_at, created_at, updated_at, yifan_orderno, delivery_status,
                           callback_data, chat_id
                    FROM orders
                    WHERE {' AND '.join(conditions)}
                    ORDER BY datetime(COALESCE(updated_at, created_at)) DESC, created_at DESC
                    LIMIT 1
                    """,
                    params,
                )
                row = cursor.fetchone()
                return self._order_row_to_dict(row) if row else None
            except Exception as e:
                logger.error(
                    f"根据 buyer_id 获取最近订单失败: buyer_id={buyer_id}, account_id={account_id}, error={e}"
                )
                return None

    def get_recent_order_by_sid(self, sid: str, *, account_id: str, status: str = None, minutes: int = 10):
        normalized_account_id = self._require_account_id(account_id)
        with self.lock:
            try:
                cursor = self.conn.cursor()
                sid_clean = sid.split("@")[0] if "@" in sid else sid
                conditions = ["(sid = ? OR sid = ? OR sid LIKE ?)", "account_id = ?"]
                params = [sid, sid_clean, f"{sid_clean}@%", normalized_account_id]

                status_filters = self._expand_order_status_filter(status) if status else []
                if status_filters:
                    placeholders = ", ".join(["?"] * len(status_filters))
                    conditions.append(f"order_status IN ({placeholders})")
                    params.extend(status_filters)

                conditions.append("datetime(COALESCE(updated_at, created_at)) >= datetime('now', ?)")
                params.append(f"-{minutes} minutes")

                cursor.execute(
                    f"""
                    SELECT order_id, item_id, buyer_id, buyer_nick, sid, spec_name, spec_value,
                           spec_name_2, spec_value_2, quantity, amount, bargain_flow_detected, bargain_success_detected,
                           order_status, pre_refund_status, account_id, platform_created_at, platform_paid_at,
                           platform_completed_at, created_at, updated_at, yifan_orderno, delivery_status,
                           callback_data, chat_id
                    FROM orders
                    WHERE {' AND '.join(conditions)}
                    ORDER BY datetime(COALESCE(updated_at, created_at)) DESC, created_at DESC
                    LIMIT 1
                    """,
                    params,
                )
                row = cursor.fetchone()
                return self._order_row_to_dict(row) if row else None
            except Exception as e:
                logger.error(f"根据 sid 获取最近订单失败: sid={sid}, account_id={account_id}, error={e}")
                return None

    def find_recent_orders_by_match_context(self, *, account_id: str, sid: str = None, buyer_id: str = None,
                                            item_id: str = None, statuses: List[str] = None,
                                            exclude_order_id: str = None, minutes: int = 30, limit: int = 10):
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                conditions = ["account_id = ?"]
                params = [normalized_account_id]
                has_match_key = False

                if sid:
                    sid_clean = sid.split("@")[0] if "@" in sid else sid
                    conditions.append("(sid = ? OR sid = ? OR sid LIKE ?)")
                    params.extend([sid, sid_clean, f"{sid_clean}@%"])
                    has_match_key = True

                if buyer_id:
                    conditions.append("buyer_id = ?")
                    params.append(buyer_id)
                    has_match_key = True

                if item_id:
                    conditions.append("item_id = ?")
                    params.append(item_id)
                    has_match_key = True

                if exclude_order_id:
                    conditions.append("order_id != ?")
                    params.append(exclude_order_id)

                status_filters: List[str] = []
                for status in statuses or []:
                    for candidate in self._expand_order_status_filter(status):
                        if candidate not in status_filters:
                            status_filters.append(candidate)
                if status_filters:
                    placeholders = ", ".join(["?"] * len(status_filters))
                    conditions.append(f"order_status IN ({placeholders})")
                    params.extend(status_filters)

                if not has_match_key:
                    logger.warning("find_recent_orders_by_match_context 缺少有效匹配键，拒绝全表扫描")
                    return []

                conditions.append("datetime(COALESCE(updated_at, created_at)) >= datetime('now', ?)")
                params.append(f"-{minutes} minutes")
                params.append(max(1, min(int(limit), 100)))

                cursor.execute(
                    f"""
                    SELECT order_id, item_id, buyer_id, buyer_nick, sid, spec_name, spec_value,
                           spec_name_2, spec_value_2, quantity, amount, bargain_flow_detected, bargain_success_detected,
                           order_status, pre_refund_status, account_id, platform_created_at, platform_paid_at,
                           platform_completed_at, created_at, updated_at, yifan_orderno, delivery_status,
                           callback_data, chat_id
                    FROM orders
                    WHERE {' AND '.join(conditions)}
                    ORDER BY datetime(COALESCE(updated_at, created_at)) DESC, created_at DESC
                    LIMIT ?
                    """,
                    params,
                )
                return [self._order_row_to_dict(row) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(
                    f"根据匹配上下文获取最近订单失败: account_id={account_id}, sid={sid}, buyer_id={buyer_id}, "
                    f"item_id={item_id}, error={e}"
                )
                return []

    def update_order_yifan_status(self, order_id: str, *, account_id: str, yifan_orderno: str = None,
                                  delivery_status: str = None, callback_data: str = None) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "SELECT account_id, order_status FROM orders WHERE order_id = ?",
                    (order_id,),
                )
                existing_order = cursor.fetchone()
                if not existing_order:
                    logger.warning(f"订单不存在，无法更新易凡状态: order_id={order_id}")
                    return False

                existing_account_id = str(existing_order[0] or "").strip()
                current_order_status = existing_order[1]
                if not existing_account_id or existing_account_id != normalized_account_id:
                    logger.warning(
                        f"拒绝跨账号更新易凡状态: order_id={order_id}, existing_account_id={existing_order[0]}, "
                        f"incoming_account_id={normalized_account_id}"
                    )
                    return False

                update_fields = []
                update_values = []

                if yifan_orderno is not None:
                    update_fields.append("yifan_orderno = ?")
                    update_values.append(yifan_orderno)

                if delivery_status is not None:
                    update_fields.append("delivery_status = ?")
                    update_values.append(delivery_status)

                    merged_order_status = self.resolve_external_order_status(
                        current_order_status,
                        delivery_status,
                        source="yifan_status",
                    )
                    normalized_current_status = self._normalize_order_status(current_order_status)
                    if merged_order_status and merged_order_status != normalized_current_status:
                        update_fields.append("order_status = ?")
                        update_values.append(merged_order_status)

                if callback_data is not None:
                    update_fields.append("callback_data = ?")
                    update_values.append(callback_data)

                update_fields.append("updated_at = CURRENT_TIMESTAMP")
                update_values.extend([order_id, normalized_account_id])

                cursor.execute(
                    f"UPDATE orders SET {', '.join(update_fields)} WHERE order_id = ? AND account_id = ?",
                    update_values,
                )
                if cursor.rowcount <= 0:
                    self.conn.rollback()
                    return False

                self.conn.commit()
                logger.info(f"更新订单易凡状态成功: order_id={order_id}, account_id={normalized_account_id}")
                return True
            except Exception as e:
                logger.error(
                    f"更新订单易凡状态失败: order_id={order_id}, account_id={account_id}, error={e}"
                )
                self.conn.rollback()
                raise

    def get_order_info(self, order_id: str, *, account_id: str):
        return self.get_order_by_id(order_id, account_id=account_id)

    def get_order_by_yifan_orderno(self, yifan_orderno: str, *, account_id: str):
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT order_id, item_id, buyer_id, buyer_nick, sid, spec_name, spec_value,
                           spec_name_2, spec_value_2, quantity, amount, bargain_flow_detected, bargain_success_detected,
                           order_status, pre_refund_status, account_id, platform_created_at, platform_paid_at,
                           platform_completed_at, created_at, updated_at, yifan_orderno, delivery_status,
                           callback_data, chat_id
                    FROM orders
                    WHERE yifan_orderno = ? AND account_id = ?
                    ORDER BY datetime(COALESCE(updated_at, created_at)) DESC, created_at DESC
                    LIMIT 1
                    """,
                    (yifan_orderno, normalized_account_id),
                )
                row = cursor.fetchone()
                return self._order_row_to_dict(row) if row else None
            except Exception as e:
                logger.error(
                    f"根据易凡订单号获取订单失败: yifan_orderno={yifan_orderno}, account_id={account_id}, error={e}"
                )
                raise

    def update_order_chat_id(self, order_id: str, chat_id: str, *, account_id: str) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    UPDATE orders
                    SET chat_id = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE order_id = ? AND account_id = ?
                    """,
                    (chat_id, order_id, normalized_account_id),
                )
                if cursor.rowcount <= 0:
                    self.conn.rollback()
                    logger.warning(
                        f"更新订单 chat_id 失败，订单不存在或账号不匹配: order_id={order_id}, account_id={normalized_account_id}"
                    )
                    return False
                self.conn.commit()
                return True
            except Exception as e:
                logger.error(f"更新订单 chat_id 失败: order_id={order_id}, account_id={account_id}, error={e}")
                self.conn.rollback()
                raise


    def _item_row_to_dict(self, row: Tuple[Any, ...]) -> Dict[str, Any]:
        item_info = {
            "id": row[0],
            "account_id": row[1],
            "item_id": row[2],
            "item_title": row[3],
            "item_description": row[4],
            "item_category": row[5],
            "item_price": row[6],
            "item_detail": row[7],
            "is_multi_spec": bool(row[8]) if row[8] is not None else False,
            "multi_quantity_delivery": bool(row[9]) if row[9] is not None else False,
            "created_at": row[10],
            "updated_at": row[11],
        }
        if item_info["item_detail"]:
            try:
                item_info["item_detail_parsed"] = json.loads(item_info["item_detail"])
            except Exception:
                item_info["item_detail_parsed"] = {}
        return item_info

    def save_item_info(self, account_id: str, item_id: str, item_data=None) -> bool:
        normalized_account_id = self._require_account_id(account_id)
        if not item_id:
            return False

        item_data = item_data or {}
        item_title = item_data.get("item_title", item_data.get("title", ""))
        item_description = item_data.get("item_description", item_data.get("description", ""))
        item_category = item_data.get("item_category", item_data.get("category", ""))
        item_price = item_data.get("item_price", item_data.get("price", ""))
        item_detail = item_data.get("item_detail", item_data.get("detail", ""))
        is_multi_spec = 1 if item_data.get("is_multi_spec", False) else 0
        multi_quantity_delivery = 1 if item_data.get("multi_quantity_delivery", False) else 0

        if item_detail is None:
            item_detail = ""
        elif not isinstance(item_detail, str):
            item_detail = json.dumps(item_detail, ensure_ascii=False)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO item_info (
                        account_id, item_id, item_title, item_description, item_category,
                        item_price, item_detail, is_multi_spec, multi_quantity_delivery,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(account_id, item_id) DO UPDATE SET
                        item_title = excluded.item_title,
                        item_description = excluded.item_description,
                        item_category = excluded.item_category,
                        item_price = excluded.item_price,
                        item_detail = excluded.item_detail,
                        is_multi_spec = excluded.is_multi_spec,
                        multi_quantity_delivery = excluded.multi_quantity_delivery,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        normalized_account_id,
                        item_id,
                        item_title,
                        item_description,
                        item_category,
                        item_price,
                        item_detail,
                        is_multi_spec,
                        multi_quantity_delivery,
                    ),
                )
                self.conn.commit()
                return True
            except Exception as e:
                logger.error(f"保存商品信息失败: account_id={account_id}, item_id={item_id}, error={e}")
                self.conn.rollback()
                raise

    def get_item_info(self, account_id: str, item_id: str) -> Optional[Dict]:
        normalized_account_id = self._require_account_id(account_id)
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT id, account_id, item_id, item_title, item_description, item_category,
                           item_price, item_detail, is_multi_spec, multi_quantity_delivery,
                           created_at, updated_at
                    FROM item_info
                    WHERE account_id = ? AND item_id = ?
                    """,
                    (normalized_account_id, item_id),
                )
                row = cursor.fetchone()
                return self._item_row_to_dict(row) if row else None
            except Exception as e:
                logger.error(f"获取商品信息失败: account_id={account_id}, item_id={item_id}, error={e}")
                raise

    def get_items_by_account(self, account_id: str) -> List[Dict]:
        normalized_account_id = self._require_account_id(account_id)
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT id, account_id, item_id, item_title, item_description, item_category,
                           item_price, item_detail, is_multi_spec, multi_quantity_delivery,
                           created_at, updated_at
                    FROM item_info
                    WHERE account_id = ?
                    ORDER BY datetime(updated_at) DESC, id DESC
                    """,
                    (normalized_account_id,),
                )
                return [self._item_row_to_dict(row) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"获取账号商品列表失败: account_id={account_id}, error={e}")
                raise

    def count_items_by_account(self, account_id: str) -> int:
        normalized_account_id = self._require_account_id(account_id)
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM item_info
                    WHERE account_id = ?
                    """,
                    (normalized_account_id,),
                )
                row = cursor.fetchone()
                return int(row[0] or 0) if row else 0
            except Exception as e:
                logger.error(f"统计账号商品数量失败: account_id={account_id}, error={e}")
                raise

    def batch_save_item_basic_info(self, items_data: list) -> int:
        if not items_data:
            return 0

        success_count = 0
        with self.lock:
            try:
                cursor = self.conn.cursor()
                for item_data in items_data:
                    account_id = str(item_data.get("account_id") or "").strip()
                    item_id = str(item_data.get("item_id") or "").strip()
                    item_title = str(item_data.get("item_title") or "").strip()
                    if not account_id or not item_id or not item_title:
                        continue

                    cursor.execute(
                        """
                        INSERT INTO item_info (
                            account_id, item_id, item_title, item_description, item_category,
                            item_price, item_detail, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        ON CONFLICT(account_id, item_id) DO UPDATE SET
                            item_title = excluded.item_title,
                            item_description = CASE
                                WHEN excluded.item_description != '' THEN excluded.item_description
                                ELSE item_info.item_description
                            END,
                            item_category = CASE
                                WHEN excluded.item_category != '' THEN excluded.item_category
                                ELSE item_info.item_category
                            END,
                            item_price = CASE
                                WHEN excluded.item_price != '' THEN excluded.item_price
                                ELSE item_info.item_price
                            END,
                            item_detail = CASE
                                WHEN excluded.item_detail != '' THEN excluded.item_detail
                                ELSE item_info.item_detail
                            END,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (
                            account_id,
                            item_id,
                            item_title,
                            item_data.get("item_description", ""),
                            item_data.get("item_category", ""),
                            item_data.get("item_price", ""),
                            item_data.get("item_detail", ""),
                        ),
                    )
                    success_count += 1
                self.conn.commit()
                return success_count
            except Exception as e:
                logger.error(f"批量保存商品基础信息失败: {e}")
                self.conn.rollback()
                raise

    def batch_update_item_title_price(self, items_data: list) -> int:
        if not items_data:
            return 0

        success_count = 0
        with self.lock:
            try:
                cursor = self.conn.cursor()
                for item_data in items_data:
                    account_id = str(item_data.get("account_id") or "").strip()
                    item_id = str(item_data.get("item_id") or "").strip()
                    if not account_id or not item_id:
                        continue

                    cursor.execute(
                        """
                        UPDATE item_info
                        SET item_title = ?, item_price = ?, item_category = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE account_id = ? AND item_id = ?
                        """,
                        (
                            item_data.get("item_title", ""),
                            item_data.get("item_price", ""),
                            item_data.get("item_category", ""),
                            account_id,
                            item_id,
                        ),
                    )
                    if cursor.rowcount > 0:
                        success_count += 1
                self.conn.commit()
                return success_count
            except Exception as e:
                logger.error(f"批量更新商品标题价格失败: {e}")
                self.conn.rollback()
                raise

    def delete_item_info(self, account_id: str, item_id: str) -> bool:
        normalized_account_id = self._require_account_id(account_id)
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "DELETE FROM item_info WHERE account_id = ? AND item_id = ?",
                    (normalized_account_id, item_id),
                )
                if cursor.rowcount > 0:
                    self.conn.commit()
                    return True
                self.conn.rollback()
                return False
            except Exception as e:
                logger.error(f"删除商品信息失败: account_id={account_id}, item_id={item_id}, error={e}")
                self.conn.rollback()
                raise

    def batch_delete_item_info(self, items_to_delete: list) -> int:
        if not items_to_delete:
            return 0

        success_count = 0
        with self.lock:
            try:
                cursor = self.conn.cursor()
                for item_data in items_to_delete:
                    account_id = str(item_data.get("account_id") or "").strip()
                    item_id = str(item_data.get("item_id") or "").strip()
                    if not account_id or not item_id:
                        continue
                    cursor.execute(
                        "DELETE FROM item_info WHERE account_id = ? AND item_id = ?",
                        (account_id, item_id),
                    )
                    if cursor.rowcount > 0:
                        success_count += 1
                self.conn.commit()
                return success_count
            except Exception as e:
                logger.error(f"批量删除商品信息失败: {e}")
                self.conn.rollback()
                raise

    def get_item_replay(self, account_id: str, item_id: str) -> Optional[Dict[str, Any]]:
        normalized_account_id = self._require_account_id(account_id)
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT item_id, account_id, reply_content, created_at, updated_at
                    FROM item_replay
                    WHERE account_id = ? AND item_id = ?
                    """,
                    (normalized_account_id, item_id),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                return {
                    "item_id": row[0],
                    "account_id": row[1],
                    "reply_content": row[2] or "",
                    "created_at": row[3],
                    "updated_at": row[4],
                }
            except Exception as e:
                logger.error(f"获取商品回复失败: account_id={account_id}, item_id={item_id}, error={e}")
                return None

    def get_item_reply(self, account_id: str, item_id: str) -> Optional[Dict[str, Any]]:
        return self.get_item_replay(account_id, item_id)

    def update_item_reply(self, account_id: str, item_id: str, reply_content: str) -> bool:
        normalized_account_id = self._require_account_id(account_id)
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO item_replay (item_id, account_id, reply_content, created_at, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(account_id, item_id) DO UPDATE SET
                        reply_content = excluded.reply_content,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (item_id, normalized_account_id, reply_content),
                )
                self.conn.commit()
                return True
            except Exception as e:
                logger.error(f"更新商品回复失败: account_id={account_id}, item_id={item_id}, error={e}")
                self.conn.rollback()
                return False

    def get_item_replays_by_account(self, account_id: str) -> List[Dict[str, Any]]:
        normalized_account_id = self._require_account_id(account_id)
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT r.item_id, r.account_id, r.reply_content, r.created_at, r.updated_at,
                           i.item_title, i.item_detail
                    FROM item_replay r
                    LEFT JOIN item_info i ON i.account_id = r.account_id AND i.item_id = r.item_id
                    WHERE r.account_id = ?
                    ORDER BY datetime(r.updated_at) DESC, r.id DESC
                    """,
                    (normalized_account_id,),
                )
                results = []
                for row in cursor.fetchall():
                    results.append(
                        {
                            "item_id": row[0],
                            "account_id": row[1],
                            "reply_content": row[2] or "",
                            "created_at": row[3],
                            "updated_at": row[4],
                            "item_title": row[5],
                            "item_detail": row[6],
                        }
                    )
                return results
            except Exception as e:
                logger.error(f"获取账号商品回复列表失败: account_id={account_id}, error={e}")
                return []

    def delete_item_reply(self, account_id: str, item_id: str) -> bool:
        normalized_account_id = self._require_account_id(account_id)
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "DELETE FROM item_replay WHERE account_id = ? AND item_id = ?",
                    (normalized_account_id, item_id),
                )
                if cursor.rowcount > 0:
                    self.conn.commit()
                    return True
                self.conn.rollback()
                return False
            except Exception as e:
                logger.error(f"删除商品回复失败: account_id={account_id}, item_id={item_id}, error={e}")
                self.conn.rollback()
                return False

    def batch_delete_item_replies(self, items: List[Dict[str, str]]) -> Dict[str, int]:
        success_count = 0
        failed_count = 0

        with self.lock:
            try:
                cursor = self.conn.cursor()
                for item in items:
                    account_id = str(item.get("account_id") or "").strip()
                    item_id = str(item.get("item_id") or "").strip()
                    if not account_id or not item_id:
                        failed_count += 1
                        continue
                    cursor.execute(
                        "DELETE FROM item_replay WHERE account_id = ? AND item_id = ?",
                        (account_id, item_id),
                    )
                    if cursor.rowcount > 0:
                        success_count += 1
                    else:
                        failed_count += 1
                self.conn.commit()
            except Exception as e:
                logger.error(f"批量删除商品回复失败: {e}")
                self.conn.rollback()
                return {"success_count": 0, "failed_count": len(items)}

        return {"success_count": success_count, "failed_count": failed_count}


    def _default_ai_reply_settings(self) -> Dict[str, Any]:
        return {
            "ai_enabled": False,
            "model_name": "qwen-plus",
            "api_key": "",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_type": "",
            "max_discount_percent": 10,
            "max_discount_amount": 100,
            "max_bargain_rounds": 3,
            "custom_prompts": "",
        }

    def _comment_template_row_to_dict(self, row: Tuple[Any, ...]) -> Dict[str, Any]:
        return {
            "id": row[0],
            "account_id": row[1],
            "name": row[2],
            "content": row[3],
            "is_active": bool(row[4]) if row[4] is not None else False,
            "sort_order": row[5] if row[5] is not None else 0,
            "created_at": row[6],
            "updated_at": row[7],
        }

    def save_keywords(self, account_id: str, keywords: List[Tuple[str, str]]) -> bool:
        return self.save_keywords_with_item_id(
            account_id,
            [(keyword, reply, None) for keyword, reply in keywords],
        )

    def save_keywords_with_item_id(self, account_id: str, keywords: List[Tuple[str, str, str]]) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("DELETE FROM keywords WHERE account_id = ?", (normalized_account_id,))

                for keyword, reply, item_id in keywords:
                    normalized_item_id = str(item_id).strip() if item_id is not None and str(item_id).strip() else None
                    cursor.execute(
                        """
                        INSERT INTO keywords (account_id, keyword, reply, item_id, type, image_url)
                        VALUES (?, ?, ?, ?, 'text', NULL)
                        """,
                        (normalized_account_id, keyword, reply, normalized_item_id),
                    )

                self.conn.commit()
                return True
            except Exception as e:
                logger.error(f"保存关键词失败: account_id={account_id}, error={e}")
                self.conn.rollback()
                return False

    def save_text_keywords_only(self, account_id: str, keywords: List[Tuple[str, str, str]]) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()

                for keyword, _, item_id in keywords:
                    normalized_item_id = str(item_id).strip() if item_id is not None and str(item_id).strip() else None
                    if normalized_item_id:
                        cursor.execute(
                            """
                            SELECT 1
                            FROM keywords
                            WHERE account_id = ? AND keyword = ? AND item_id = ? AND COALESCE(type, 'text') = 'image'
                            LIMIT 1
                            """,
                            (normalized_account_id, keyword, normalized_item_id),
                        )
                    else:
                        cursor.execute(
                            """
                            SELECT 1
                            FROM keywords
                            WHERE account_id = ? AND keyword = ? AND (item_id IS NULL OR TRIM(item_id) = '') AND COALESCE(type, 'text') = 'image'
                            LIMIT 1
                            """,
                            (normalized_account_id, keyword),
                        )
                    if cursor.fetchone():
                        item_desc = f"item_id={normalized_item_id}" if normalized_item_id else "item_id=<general>"
                        raise ValueError(f"关键词 {keyword!r} 与已有图片关键词冲突: {item_desc}")

                cursor.execute(
                    """
                    DELETE FROM keywords
                    WHERE account_id = ? AND COALESCE(type, 'text') = 'text'
                    """,
                    (normalized_account_id,),
                )

                for keyword, reply, item_id in keywords:
                    normalized_item_id = str(item_id).strip() if item_id is not None and str(item_id).strip() else None
                    cursor.execute(
                        """
                        INSERT INTO keywords (account_id, keyword, reply, item_id, type, image_url)
                        VALUES (?, ?, ?, ?, 'text', NULL)
                        """,
                        (normalized_account_id, keyword, reply, normalized_item_id),
                    )

                self.conn.commit()
                return True
            except ValueError:
                self.conn.rollback()
                raise
            except Exception as e:
                logger.error(f"保存文本关键词失败: account_id={account_id}, error={e}")
                self.conn.rollback()
                return False

    def get_keywords(self, account_id: str) -> List[Tuple[str, str]]:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT keyword, reply
                    FROM keywords
                    WHERE account_id = ?
                    ORDER BY rowid
                    """,
                    (normalized_account_id,),
                )
                return [(row[0], row[1]) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"获取关键词失败: account_id={account_id}, error={e}")
                return []

    def get_keywords_with_item_id(self, account_id: str) -> List[Tuple[str, str, str]]:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT keyword, reply, item_id
                    FROM keywords
                    WHERE account_id = ?
                    ORDER BY rowid
                    """,
                    (normalized_account_id,),
                )
                return [(row[0], row[1], row[2]) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"获取带商品ID关键词失败: account_id={account_id}, error={e}")
                return []

    def get_keyword_counts(self, user_id: int = None) -> Dict[str, int]:
        """按账号汇总关键词数量；支持按用户过滤。"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    cursor.execute(
                        """
                        SELECT k.account_id, COUNT(*)
                        FROM keywords k
                        JOIN cookies c ON k.account_id = c.id
                        WHERE c.user_id = ?
                        GROUP BY k.account_id
                        """,
                        (user_id,),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT account_id, COUNT(*)
                        FROM keywords
                        GROUP BY account_id
                        """
                    )
                return {
                    str(row[0]): int(row[1] or 0)
                    for row in cursor.fetchall()
                    if row and row[0]
                }
            except Exception as e:
                logger.error(f"获取关键词数量汇总失败: error={e}")
                raise

    def check_keyword_duplicate(self, account_id: str, keyword: str, item_id: str = None) -> bool:
        normalized_account_id = self._require_account_id(account_id)
        normalized_item_id = str(item_id).strip() if item_id is not None and str(item_id).strip() else None

        with self.lock:
            try:
                cursor = self.conn.cursor()
                if normalized_item_id:
                    cursor.execute(
                        """
                        SELECT COUNT(*)
                        FROM keywords
                        WHERE account_id = ? AND keyword = ? AND item_id = ?
                        """,
                        (normalized_account_id, keyword, normalized_item_id),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT COUNT(*)
                        FROM keywords
                        WHERE account_id = ? AND keyword = ? AND (item_id IS NULL OR TRIM(item_id) = '')
                        """,
                        (normalized_account_id, keyword),
                    )
                result = cursor.fetchone()
                return bool(result and result[0] > 0)
            except Exception as e:
                logger.error(f"检查关键词重复失败: account_id={account_id}, error={e}")
                return False

    def save_image_keyword(self, account_id: str, keyword: str, image_url: str, item_id: str = None) -> bool:
        normalized_account_id = self._require_account_id(account_id)
        normalized_item_id = str(item_id).strip() if item_id is not None and str(item_id).strip() else None

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO keywords (account_id, keyword, reply, item_id, type, image_url)
                    VALUES (?, ?, '', ?, 'image', ?)
                    """,
                    (normalized_account_id, keyword, normalized_item_id, image_url),
                )
                self.conn.commit()
                return True
            except Exception as e:
                logger.error(f"保存图片关键词失败: account_id={account_id}, keyword={keyword}, error={e}")
                self.conn.rollback()
                return False

    def count_keywords_by_image_url(self, account_id: str, image_url: str) -> int:
        normalized_account_id = self._require_account_id(account_id)
        normalized_image_url = (image_url or "").strip()
        if not normalized_image_url:
            return 0

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM keywords
                    WHERE account_id = ?
                      AND COALESCE(type, 'text') = 'image'
                      AND image_url = ?
                    """,
                    (normalized_account_id, normalized_image_url),
                )
                row = cursor.fetchone()
                return int(row[0]) if row else 0
            except Exception as e:
                logger.error(
                    f"统计图片关键词引用数量失败: account_id={account_id}, image_url={image_url}, error={e}"
                )
                return -1

    def get_keywords_with_type(self, account_id: str) -> List[Dict[str, Any]]:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT k.keyword, k.reply, k.item_id, COALESCE(k.type, 'text'), k.image_url, i.item_title
                    FROM keywords k
                    LEFT JOIN item_info i ON i.account_id = k.account_id AND i.item_id = k.item_id
                    WHERE k.account_id = ?
                    ORDER BY k.rowid
                    """,
                    (normalized_account_id,),
                )
                return [
                    {
                        "keyword": row[0],
                        "reply": row[1],
                        "item_id": row[2],
                        "type": row[3],
                        "image_url": row[4],
                        "item_title": row[5],
                    }
                    for row in cursor.fetchall()
                ]
            except Exception as e:
                logger.error(f"获取带类型的关键词失败: account_id={account_id}, error={e}")
                return []

    def update_keyword_image_url(self, account_id: str, keyword: str, new_image_url: str) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    UPDATE keywords
                    SET image_url = ?
                    WHERE account_id = ? AND keyword = ? AND COALESCE(type, 'text') = 'image'
                    """,
                    (new_image_url, normalized_account_id, keyword),
                )
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"更新关键词图片URL失败: account_id={account_id}, keyword={keyword}, error={e}")
                self.conn.rollback()
                return False

    def delete_keyword_by_index(self, account_id: str, index: int) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT rowid
                    FROM keywords
                    WHERE account_id = ?
                    ORDER BY rowid
                    """,
                    (normalized_account_id,),
                )
                rows = cursor.fetchall()
                if index < 0 or index >= len(rows):
                    return False

                cursor.execute("DELETE FROM keywords WHERE rowid = ?", (rows[index][0],))
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"按索引删除关键字失败: account_id={account_id}, index={index}, error={e}")
                self.conn.rollback()
                return False

    def save_cookie_status(self, account_id: str, enabled: bool):
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO cookie_status (account_id, enabled, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(account_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (normalized_account_id, int(enabled)),
            )
            self.conn.commit()
            return True

    def get_cookie_status(self, account_id: str) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("SELECT enabled FROM cookie_status WHERE account_id = ?", (normalized_account_id,))
                result = cursor.fetchone()
                return bool(result[0]) if result else True
            except Exception as e:
                logger.error(f"获取账号启用状态失败: account_id={account_id}, error={e}")
                raise

    def get_all_cookie_status(self) -> Dict[str, bool]:
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("SELECT account_id, enabled FROM cookie_status")
                return {row[0]: bool(row[1]) for row in cursor.fetchall()}
            except Exception as e:
                logger.error(f"获取全部账号启用状态失败: error={e}")
                raise

    def save_ai_reply_settings(self, account_id: str, settings: dict) -> bool:
        normalized_account_id = self._require_account_id(account_id)
        payload = dict(self._default_ai_reply_settings())
        payload.update(settings or {})

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO ai_reply_settings (
                        account_id, ai_enabled, model_name, api_key, base_url, api_type,
                        max_discount_percent, max_discount_amount, max_bargain_rounds,
                        custom_prompts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(account_id) DO UPDATE SET
                        ai_enabled = excluded.ai_enabled,
                        model_name = excluded.model_name,
                        api_key = excluded.api_key,
                        base_url = excluded.base_url,
                        api_type = excluded.api_type,
                        max_discount_percent = excluded.max_discount_percent,
                        max_discount_amount = excluded.max_discount_amount,
                        max_bargain_rounds = excluded.max_bargain_rounds,
                        custom_prompts = excluded.custom_prompts,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        normalized_account_id,
                        int(bool(payload.get("ai_enabled", False))),
                        payload.get("model_name", "qwen-plus"),
                        payload.get("api_key", ""),
                        payload.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                        payload.get("api_type", ""),
                        int(payload.get("max_discount_percent", 10)),
                        int(payload.get("max_discount_amount", 100)),
                        int(payload.get("max_bargain_rounds", 3)),
                        payload.get("custom_prompts", ""),
                    ),
                )
                self.conn.commit()
                return True
            except Exception as e:
                logger.error(f"保存 AI 回复设置失败: account_id={account_id}, error={e}")
                self.conn.rollback()
                raise

    def get_ai_reply_settings(self, account_id: str) -> dict:
        normalized_account_id = self._require_account_id(account_id)
        defaults = self._default_ai_reply_settings()

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT ai_enabled, model_name, api_key, base_url, api_type,
                           max_discount_percent, max_discount_amount, max_bargain_rounds, custom_prompts
                    FROM ai_reply_settings
                    WHERE account_id = ?
                    """,
                    (normalized_account_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return defaults

                return {
                    "ai_enabled": bool(row[0]),
                    "model_name": row[1] or defaults["model_name"],
                    "api_key": row[2] or "",
                    "base_url": row[3] or defaults["base_url"],
                    "api_type": row[4] or "",
                    "max_discount_percent": row[5] if row[5] is not None else defaults["max_discount_percent"],
                    "max_discount_amount": row[6] if row[6] is not None else defaults["max_discount_amount"],
                    "max_bargain_rounds": row[7] if row[7] is not None else defaults["max_bargain_rounds"],
                    "custom_prompts": row[8] or "",
                }
            except Exception as e:
                logger.error(f"获取 AI 回复设置失败: account_id={account_id}, error={e}")
                raise

    def get_all_ai_reply_settings(self, user_id: int = None) -> Dict[str, dict]:
        defaults = self._default_ai_reply_settings()

        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    cursor.execute(
                        """
                        SELECT ars.account_id, ars.ai_enabled, ars.model_name, ars.api_key, ars.base_url, ars.api_type,
                               ars.max_discount_percent, ars.max_discount_amount, ars.max_bargain_rounds, ars.custom_prompts
                        FROM ai_reply_settings ars
                        JOIN cookies c ON ars.account_id = c.id
                        WHERE c.user_id = ?
                        """,
                        (user_id,),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT account_id, ai_enabled, model_name, api_key, base_url, api_type,
                               max_discount_percent, max_discount_amount, max_bargain_rounds, custom_prompts
                        FROM ai_reply_settings
                        """
                    )

                result = {}
                for row in cursor.fetchall():
                    result[row[0]] = {
                        "ai_enabled": bool(row[1]),
                        "model_name": row[2] or defaults["model_name"],
                        "api_key": row[3] or "",
                        "base_url": row[4] or defaults["base_url"],
                        "api_type": row[5] or "",
                        "max_discount_percent": row[6] if row[6] is not None else defaults["max_discount_percent"],
                        "max_discount_amount": row[7] if row[7] is not None else defaults["max_discount_amount"],
                        "max_bargain_rounds": row[8] if row[8] is not None else defaults["max_bargain_rounds"],
                        "custom_prompts": row[9] or "",
                    }

                return result
            except Exception as e:
                logger.error(f"获取全部 AI 回复设置失败: error={e}")
                raise

    def save_default_reply(self, account_id: str, enabled: bool, reply_content: str = None, reply_once: bool = False):
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO default_replies (account_id, enabled, reply_content, reply_once, created_at, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(account_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    reply_content = excluded.reply_content,
                    reply_once = excluded.reply_once,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (normalized_account_id, int(enabled), reply_content, int(bool(reply_once))),
            )
            self.conn.commit()
            return True

    def get_default_reply(self, account_id: str) -> Optional[Dict[str, Any]]:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT account_id, enabled, reply_content, reply_once, created_at, updated_at
                    FROM default_replies
                    WHERE account_id = ?
                    """,
                    (normalized_account_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                return {
                    "account_id": row[0],
                    "enabled": bool(row[1]),
                    "reply_content": row[2],
                    "reply_once": bool(row[3]) if row[3] is not None else False,
                    "created_at": row[4],
                    "updated_at": row[5],
                }
            except Exception as e:
                logger.error(f"获取默认回复失败: account_id={account_id}, error={e}")
                raise

    def get_all_default_replies(self, user_id: int = None) -> Dict[str, Dict[str, Any]]:
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    cursor.execute(
                        """
                        SELECT dr.account_id, dr.enabled, dr.reply_content, dr.reply_once, dr.created_at, dr.updated_at
                        FROM default_replies dr
                        JOIN cookies c ON dr.account_id = c.id
                        WHERE c.user_id = ?
                        """,
                        (user_id,),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT account_id, enabled, reply_content, reply_once, created_at, updated_at
                        FROM default_replies
                        """
                    )
                result = {}
                for row in cursor.fetchall():
                    result[row[0]] = {
                        "enabled": bool(row[1]),
                        "reply_content": row[2],
                        "reply_once": bool(row[3]) if row[3] is not None else False,
                        "created_at": row[4],
                        "updated_at": row[5],
                    }
                return result
            except Exception as e:
                logger.error(f"获取全部默认回复失败: error={e}")
                raise

    def add_default_reply_record(self, account_id: str, chat_id: str):
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT OR IGNORE INTO default_reply_records (account_id, chat_id, replied_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                """,
                (normalized_account_id, chat_id),
            )
            self.conn.commit()
            return True

    def has_default_reply_record(self, account_id: str, chat_id: str) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT 1
                    FROM default_reply_records
                    WHERE account_id = ? AND chat_id = ?
                    LIMIT 1
                    """,
                    (normalized_account_id, chat_id),
                )
                return cursor.fetchone() is not None
            except Exception as e:
                logger.error(f"检查默认回复记录失败: account_id={account_id}, chat_id={chat_id}, error={e}")
                return False

    def clear_default_reply_records(self, account_id: str):
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM default_reply_records WHERE account_id = ?", (normalized_account_id,))
            self.conn.commit()
            return True

    def delete_default_reply(self, account_id: str) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("DELETE FROM default_replies WHERE account_id = ?", (normalized_account_id,))
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"删除默认回复失败: account_id={account_id}, error={e}")
                self.conn.rollback()
                raise

    def set_message_notification(self, account_id: str, channel_id: int, enabled: bool = True) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO message_notifications (account_id, channel_id, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(account_id, channel_id) DO UPDATE SET
                        enabled = excluded.enabled,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (normalized_account_id, channel_id, int(enabled)),
                )
                self.conn.commit()
                return True
            except Exception as e:
                logger.error(f"保存消息通知设置失败: account_id={account_id}, channel_id={channel_id}, error={e}")
                self.conn.rollback()
                raise

    def replace_account_notifications(
        self,
        account_id: str,
        channel_ids: List[int],
        *,
        enabled: bool = True,
        user_id: int = None,
    ) -> int:
        normalized_account_id = self._require_account_id(account_id)
        normalized_channel_ids: List[int] = []
        seen_channel_ids = set()

        for channel_id in channel_ids or []:
            try:
                normalized_channel_id = int(channel_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("通知渠道ID无效") from exc

            if normalized_channel_id <= 0:
                raise ValueError("通知渠道ID无效")
            if normalized_channel_id in seen_channel_ids:
                continue

            seen_channel_ids.add(normalized_channel_id)
            normalized_channel_ids.append(normalized_channel_id)

        if not normalized_channel_ids:
            raise ValueError("请选择通知渠道")

        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    placeholders = ", ".join(["?"] * len(normalized_channel_ids))
                    cursor.execute(
                        f"""
                        SELECT id
                        FROM notification_channels
                        WHERE user_id = ? AND id IN ({placeholders})
                        """,
                        (user_id, *normalized_channel_ids),
                    )
                    existing_channel_ids = {int(row[0]) for row in cursor.fetchall()}
                    missing_channel_ids = [
                        channel_id for channel_id in normalized_channel_ids if channel_id not in existing_channel_ids
                    ]
                    if missing_channel_ids:
                        raise ValueError("通知渠道不存在")

                cursor.execute(
                    "DELETE FROM message_notifications WHERE account_id = ?",
                    (normalized_account_id,),
                )
                cursor.executemany(
                    """
                    INSERT INTO message_notifications (account_id, channel_id, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    [
                        (normalized_account_id, channel_id, int(enabled))
                        for channel_id in normalized_channel_ids
                    ],
                )
                self.conn.commit()
                return len(normalized_channel_ids)
            except Exception as e:
                logger.error(
                    f"替换账号消息通知设置失败: account_id={account_id}, channel_ids={channel_ids}, error={e}"
                )
                self.conn.rollback()
                raise

    def get_account_notifications(self, account_id: str) -> List[Dict[str, Any]]:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT mn.id, mn.account_id, mn.channel_id, mn.enabled, mn.created_at, mn.updated_at,
                           nc.name, nc.type, nc.config, nc.enabled
                    FROM message_notifications mn
                    JOIN notification_channels nc ON mn.channel_id = nc.id
                    JOIN cookies c ON mn.account_id = c.id
                    WHERE mn.account_id = ? AND nc.user_id = c.user_id
                    ORDER BY mn.id
                    """,
                    (normalized_account_id,),
                )
                return [
                    {
                        "id": row[0],
                        "account_id": row[1],
                        "channel_id": row[2],
                        "enabled": bool(row[3]),
                        "created_at": row[4],
                        "updated_at": row[5],
                        "channel_name": row[6],
                        "channel_type": row[7],
                        "channel_config": row[8],
                        "channel_enabled": bool(row[9]) if row[9] is not None else False,
                    }
                    for row in cursor.fetchall()
                ]
            except Exception as e:
                logger.error(f"获取账号消息通知设置失败: account_id={account_id}, error={e}")
                raise

    def get_all_message_notifications(self, user_id: int = None) -> Dict[str, List[Dict[str, Any]]]:
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    cursor.execute(
                        """
                        SELECT mn.id, mn.account_id, mn.channel_id, mn.enabled, mn.created_at, mn.updated_at,
                               nc.name, nc.type, nc.config, nc.enabled
                        FROM message_notifications mn
                        JOIN notification_channels nc ON mn.channel_id = nc.id
                        JOIN cookies c ON mn.account_id = c.id
                        WHERE nc.user_id = c.user_id AND c.user_id = ?
                        ORDER BY mn.account_id, mn.id
                        """,
                        (user_id,),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT mn.id, mn.account_id, mn.channel_id, mn.enabled, mn.created_at, mn.updated_at,
                               nc.name, nc.type, nc.config, nc.enabled
                        FROM message_notifications mn
                        JOIN notification_channels nc ON mn.channel_id = nc.id
                        JOIN cookies c ON mn.account_id = c.id
                        WHERE nc.user_id = c.user_id
                        ORDER BY mn.account_id, mn.id
                        """
                    )

                result: Dict[str, List[Dict[str, Any]]] = {}
                for row in cursor.fetchall():
                    result.setdefault(row[1], []).append(
                        {
                            "id": row[0],
                            "account_id": row[1],
                            "channel_id": row[2],
                            "enabled": bool(row[3]),
                            "created_at": row[4],
                            "updated_at": row[5],
                            "channel_name": row[6],
                            "channel_type": row[7],
                            "channel_config": row[8],
                            "channel_enabled": bool(row[9]) if row[9] is not None else False,
                        }
                    )
                return result
            except Exception as e:
                logger.error(f"获取全部消息通知设置失败: error={e}")
                raise

    def delete_account_notifications(self, account_id: str, user_id: int = None) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    cursor.execute(
                        """
                        DELETE FROM message_notifications
                        WHERE account_id = ? AND account_id IN (
                            SELECT id FROM cookies WHERE user_id = ?
                        )
                        """,
                        (normalized_account_id, user_id),
                    )
                else:
                    cursor.execute("DELETE FROM message_notifications WHERE account_id = ?", (normalized_account_id,))
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"删除账号消息通知设置失败: account_id={account_id}, error={e}")
                self.conn.rollback()
                raise

    def update_auto_confirm(self, account_id: str, auto_confirm: bool) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("SELECT 1 FROM cookies WHERE id = ? LIMIT 1", (normalized_account_id,))
                if not cursor.fetchone():
                    return False
                cursor.execute(
                    "UPDATE cookies SET auto_confirm = ? WHERE id = ?",
                    (int(bool(auto_confirm)), normalized_account_id),
                )
                self.conn.commit()
                return True
            except Exception as e:
                logger.error(f"更新自动确认发货设置失败: account_id={account_id}, error={e}")
                self.conn.rollback()
                raise

    def update_cookie_pause_duration(self, account_id: str, pause_duration: int) -> bool:
        normalized_account_id = self._require_account_id(account_id)
        normalized_pause_duration = int(pause_duration)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("SELECT 1 FROM cookies WHERE id = ? LIMIT 1", (normalized_account_id,))
                if not cursor.fetchone():
                    return False
                cursor.execute(
                    "UPDATE cookies SET pause_duration = ? WHERE id = ?",
                    (normalized_pause_duration, normalized_account_id),
                )
                self.conn.commit()
                return True
            except Exception as e:
                logger.error(f"更新自动回复暂停时间失败: account_id={account_id}, error={e}")
                self.conn.rollback()
                raise

    def get_cookie_pause_duration(self, account_id: str) -> int:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("SELECT pause_duration FROM cookies WHERE id = ?", (normalized_account_id,))
                result = cursor.fetchone()
                if not result:
                    return 10
                if result[0] is None:
                    cursor.execute("UPDATE cookies SET pause_duration = 10 WHERE id = ?", (normalized_account_id,))
                    self.conn.commit()
                    return 10
                return int(result[0])
            except Exception as e:
                logger.error(f"获取自动回复暂停时间失败: account_id={account_id}, error={e}")
                raise

    def get_auto_confirm(self, account_id: str) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("SELECT auto_confirm FROM cookies WHERE id = ?", (normalized_account_id,))
                result = cursor.fetchone()
                return bool(result[0]) if result else True
            except Exception as e:
                logger.error(f"获取自动确认发货设置失败: account_id={account_id}, error={e}")
                raise

    def get_auto_comment(self, account_id: str) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("SELECT auto_comment FROM cookies WHERE id = ?", (normalized_account_id,))
                result = cursor.fetchone()
                if result and result[0] is not None:
                    return bool(result[0])
                return False
            except Exception as e:
                logger.error(f"获取自动好评设置失败: account_id={account_id}, error={e}")
                raise

    def update_auto_comment(self, account_id: str, auto_comment: bool) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("SELECT 1 FROM cookies WHERE id = ? LIMIT 1", (normalized_account_id,))
                if not cursor.fetchone():
                    return False
                cursor.execute(
                    "UPDATE cookies SET auto_comment = ? WHERE id = ?",
                    (int(bool(auto_comment)), normalized_account_id),
                )
                self.conn.commit()
                return True
            except Exception as e:
                logger.error(f"更新自动好评设置失败: account_id={account_id}, error={e}")
                self.conn.rollback()
                raise

    def get_comment_templates(self, account_id: str) -> List[Dict]:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT id, account_id, name, content, is_active, sort_order, created_at, updated_at
                    FROM comment_templates
                    WHERE account_id = ?
                    ORDER BY sort_order, id
                    """,
                    (normalized_account_id,),
                )
                return [self._comment_template_row_to_dict(row) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"获取好评模板列表失败: account_id={account_id}, error={e}")
                raise

    def get_active_comment_template(self, account_id: str) -> Optional[Dict]:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT id, account_id, name, content, is_active, sort_order, created_at, updated_at
                    FROM comment_templates
                    WHERE account_id = ? AND is_active = 1
                    ORDER BY sort_order, id
                    LIMIT 1
                    """,
                    (normalized_account_id,),
                )
                row = cursor.fetchone()
                return self._comment_template_row_to_dict(row) if row else None
            except Exception as e:
                logger.error(f"获取激活好评模板失败: account_id={account_id}, error={e}")
                raise

    def add_comment_template(self, account_id: str, name: str, content: str, is_active: bool = False) -> Optional[int]:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                if is_active:
                    cursor.execute("UPDATE comment_templates SET is_active = 0 WHERE account_id = ?", (normalized_account_id,))
                cursor.execute("SELECT MAX(sort_order) FROM comment_templates WHERE account_id = ?", (normalized_account_id,))
                current_max = cursor.fetchone()[0]
                sort_order = (current_max or 0) + 1
                cursor.execute(
                    """
                    INSERT INTO comment_templates (account_id, name, content, is_active, sort_order, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (normalized_account_id, name, content, int(bool(is_active)), sort_order),
                )
                self.conn.commit()
                return cursor.lastrowid
            except Exception as e:
                logger.error(f"添加好评模板失败: account_id={account_id}, name={name}, error={e}")
                self.conn.rollback()
                raise

    def update_comment_template(self, account_id: str, template_id: int, name: str = None, content: str = None, is_active: bool = None) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM comment_templates WHERE id = ? AND account_id = ? LIMIT 1",
                    (template_id, normalized_account_id),
                )
                if not cursor.fetchone():
                    return False

                if is_active:
                    cursor.execute("UPDATE comment_templates SET is_active = 0 WHERE account_id = ?", (normalized_account_id,))

                update_fields = []
                params: List[Any] = []
                if name is not None:
                    update_fields.append("name = ?")
                    params.append(name)
                if content is not None:
                    update_fields.append("content = ?")
                    params.append(content)
                if is_active is not None:
                    update_fields.append("is_active = ?")
                    params.append(int(bool(is_active)))

                if not update_fields:
                    return True

                update_fields.append("updated_at = CURRENT_TIMESTAMP")
                params.extend([template_id, normalized_account_id])
                cursor.execute(
                    f"UPDATE comment_templates SET {', '.join(update_fields)} WHERE id = ? AND account_id = ?",
                    params,
                )
                self.conn.commit()
                return True
            except Exception as e:
                logger.error(f"更新好评模板失败: account_id={account_id}, template_id={template_id}, error={e}")
                self.conn.rollback()
                raise

    def delete_comment_template(self, account_id: str, template_id: int) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "DELETE FROM comment_templates WHERE id = ? AND account_id = ?",
                    (template_id, normalized_account_id),
                )
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"删除好评模板失败: account_id={account_id}, template_id={template_id}, error={e}")
                self.conn.rollback()
                raise

    def set_active_comment_template(self, account_id: str, template_id: int) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM comment_templates WHERE id = ? AND account_id = ? LIMIT 1",
                    (template_id, normalized_account_id),
                )
                if not cursor.fetchone():
                    return False

                cursor.execute("UPDATE comment_templates SET is_active = 0 WHERE account_id = ?", (normalized_account_id,))
                cursor.execute(
                    """
                    UPDATE comment_templates
                    SET is_active = 1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND account_id = ?
                    """,
                    (template_id, normalized_account_id),
                )
                self.conn.commit()
                return True
            except Exception as e:
                logger.error(f"设置激活好评模板失败: account_id={account_id}, template_id={template_id}, error={e}")
                self.conn.rollback()
                raise

    def _delivery_log_row_to_dict(self, row: Tuple[Any, ...]) -> Dict[str, Any]:
        return {
            "id": row[0],
            "user_id": row[1],
            "account_id": row[2],
            "order_id": row[3],
            "item_id": row[4],
            "buyer_id": row[5],
            "buyer_nick": row[6],
            "rule_id": row[7],
            "rule_keyword": row[8],
            "card_type": row[9],
            "match_mode": row[10],
            "channel": row[11],
            "status": row[12],
            "reason": row[13],
            "created_at": row[14],
        }

    def _delivery_state_row_to_dict(self, row: Tuple[Any, ...]) -> Dict[str, Any]:
        delivery_meta = {}
        if row[7]:
            try:
                delivery_meta = json.loads(row[7])
            except Exception:
                delivery_meta = {}
        return {
            "order_id": row[0],
            "unit_index": row[1],
            "account_id": row[2],
            "item_id": row[3],
            "buyer_id": row[4],
            "channel": row[5],
            "status": row[6],
            "delivery_meta": delivery_meta,
            "last_error": row[8],
            "sent_at": row[9],
            "finalized_at": row[10],
            "created_at": row[11],
            "updated_at": row[12],
        }

    def _risk_control_row_to_dict(self, row: Tuple[Any, ...]) -> Dict[str, Any]:
        log_info = {
            "id": row[0],
            "account_id": row[1],
            "event_type": row[2],
            "session_id": row[3],
            "trigger_scene": row[4],
            "result_code": row[5],
            "event_description": row[6],
            "event_meta": self._decode_risk_control_event_meta(row[7]),
            "processing_result": row[8],
            "processing_status": row[9],
            "error_message": row[10],
            "duration_ms": row[11],
            "created_at": row[12],
            "updated_at": row[13],
            "account_name": row[14],
        }
        normalized = self._normalize_legacy_risk_log(log_info)
        visible_fields = (
            "id",
            "account_id",
            "event_type",
            "session_id",
            "trigger_scene",
            "result_code",
            "event_description",
            "event_meta",
            "processing_result",
            "processing_status",
            "error_message",
            "duration_ms",
            "created_at",
            "updated_at",
            "account_name",
            "event_description_display",
            "processing_result_display",
            "error_message_display",
            "is_legacy",
            "session_display",
        )
        return {
            field: normalized[field]
            for field in visible_fields
            if field in normalized
        }

    def create_delivery_log(self, user_id: int = None, account_id: str = None, order_id: str = None,
                            item_id: str = None, buyer_id: str = None, buyer_nick: str = None,
                            rule_id: int = None, rule_keyword: str = None, card_type: str = None,
                            match_mode: str = None, channel: str = 'auto', status: str = 'failed',
                            reason: str = None):
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO delivery_logs (
                        user_id, account_id, order_id, item_id, buyer_id, buyer_nick,
                        rule_id, rule_keyword, card_type, match_mode, channel, status, reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id if user_id is not None else 1,
                        normalized_account_id,
                        order_id,
                        item_id,
                        buyer_id,
                        buyer_nick,
                        rule_id,
                        rule_keyword,
                        card_type,
                        match_mode,
                        channel or "auto",
                        status or "failed",
                        reason,
                    ),
                )
                self.conn.commit()
                return cursor.lastrowid
            except Exception as e:
                logger.error(f"创建发货日志失败: account_id={account_id}, order_id={order_id}, error={e}")
                self.conn.rollback()
                return None

    def upsert_delivery_finalization_state(self, order_id: str, unit_index: int = 1, account_id: str = None,
                                           item_id: str = None, buyer_id: str = None, channel: str = 'auto',
                                           status: str = 'sent', delivery_meta: Dict[str, Any] = None,
                                           last_error: str = None):
        normalized_account_id = self._require_account_id(account_id)
        normalized_unit_index = max(1, int(unit_index or 1))
        delivery_meta_json = json.dumps(delivery_meta or {}, ensure_ascii=False)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT account_id, sent_at, finalized_at
                    FROM delivery_finalization_states
                    WHERE order_id = ? AND unit_index = ?
                    """,
                    (order_id, normalized_unit_index),
                )
                existing = cursor.fetchone()
                if existing:
                    existing_account_id = str(existing[0] or "").strip()
                    if not existing_account_id or existing_account_id != normalized_account_id:
                        self.conn.rollback()
                        return False

                    sent_at_clause = "CURRENT_TIMESTAMP" if status == "sent" and not existing[1] else "sent_at"
                    finalized_at_clause = "CURRENT_TIMESTAMP" if status == "finalized" else "finalized_at"
                    cursor.execute(
                        f"""
                        UPDATE delivery_finalization_states
                        SET account_id = ?, item_id = ?, buyer_id = ?, channel = ?, status = ?,
                            delivery_meta = ?, last_error = ?, sent_at = {sent_at_clause},
                            finalized_at = {finalized_at_clause}, updated_at = CURRENT_TIMESTAMP
                        WHERE order_id = ? AND unit_index = ? AND account_id = ?
                        """,
                        (
                            normalized_account_id,
                            item_id,
                            buyer_id,
                            channel,
                            status,
                            delivery_meta_json,
                            last_error,
                            order_id,
                            normalized_unit_index,
                            normalized_account_id,
                        ),
                    )
                else:
                    sent_at_value = "CURRENT_TIMESTAMP" if status == "sent" else "NULL"
                    finalized_at_value = "CURRENT_TIMESTAMP" if status == "finalized" else "NULL"
                    cursor.execute(
                        f"""
                        INSERT INTO delivery_finalization_states (
                            order_id, unit_index, account_id, item_id, buyer_id, channel, status,
                            delivery_meta, last_error, sent_at, finalized_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, {sent_at_value}, {finalized_at_value}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """,
                        (
                            order_id,
                            normalized_unit_index,
                            normalized_account_id,
                            item_id,
                            buyer_id,
                            channel,
                            status,
                            delivery_meta_json,
                            last_error,
                        ),
                    )

                self.conn.commit()
                return True
            except Exception as e:
                logger.error(
                    f"写入发货 finalize 状态失败: order_id={order_id}, unit_index={unit_index}, account_id={account_id}, error={e}"
                )
                self.conn.rollback()
                return False

    def get_delivery_finalization_state(self, order_id: str, unit_index: int = 1, account_id: str = None):
        normalized_account_id = self._require_account_id(account_id)
        normalized_unit_index = max(1, int(unit_index or 1))

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT order_id, unit_index, account_id, item_id, buyer_id, channel, status,
                           delivery_meta, last_error, sent_at, finalized_at, created_at, updated_at
                    FROM delivery_finalization_states
                    WHERE order_id = ? AND unit_index = ? AND account_id = ?
                    """,
                    (order_id, normalized_unit_index, normalized_account_id),
                )
                row = cursor.fetchone()
                return self._delivery_state_row_to_dict(row) if row else None
            except Exception as e:
                logger.error(
                    f"获取发货 finalize 状态失败: order_id={order_id}, unit_index={unit_index}, account_id={account_id}, error={e}"
                )
                raise

    def get_delivery_finalization_states(self, order_id: str, account_id: str = None):
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT order_id, unit_index, account_id, item_id, buyer_id, channel, status,
                           delivery_meta, last_error, sent_at, finalized_at, created_at, updated_at
                    FROM delivery_finalization_states
                    WHERE order_id = ? AND account_id = ?
                    ORDER BY unit_index ASC
                    """,
                    (order_id, normalized_account_id),
                )
                return [self._delivery_state_row_to_dict(row) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"获取发货 finalize 状态列表失败: order_id={order_id}, account_id={account_id}, error={e}")
                raise

    def get_delivery_progress_summary(self, order_id: str, account_id: str = None, expected_quantity: int = 1):
        self._require_account_id(account_id)
        try:
            expected = max(1, int(expected_quantity or 1))
        except (TypeError, ValueError):
            expected = 1

        states = self.get_delivery_finalization_states(order_id, account_id=account_id)
        state_by_unit = {}
        for state in states:
            try:
                current_unit_index = max(1, int(state.get("unit_index") or 1))
            except (TypeError, ValueError):
                current_unit_index = 1
            state_by_unit[current_unit_index] = state

        finalized_unit_indexes = []
        pending_finalize_unit_indexes = []
        remaining_unit_indexes = []

        for current_unit_index in range(1, expected + 1):
            current_status = (state_by_unit.get(current_unit_index) or {}).get("status")
            if current_status == "finalized":
                finalized_unit_indexes.append(current_unit_index)
            elif current_status == "sent":
                pending_finalize_unit_indexes.append(current_unit_index)
            else:
                remaining_unit_indexes.append(current_unit_index)

        if pending_finalize_unit_indexes:
            aggregate_status = "partial_pending_finalize"
        elif len(finalized_unit_indexes) >= expected:
            aggregate_status = "shipped"
        elif finalized_unit_indexes:
            aggregate_status = "partial_success"
        else:
            aggregate_status = "pending_ship"

        return {
            "order_id": order_id,
            "expected_quantity": expected,
            "state_count": len(states),
            "finalized_count": len(finalized_unit_indexes),
            "pending_finalize_count": len(pending_finalize_unit_indexes),
            "remaining_count": len(remaining_unit_indexes),
            "finalized_unit_indexes": finalized_unit_indexes,
            "pending_finalize_unit_indexes": pending_finalize_unit_indexes,
            "remaining_unit_indexes": remaining_unit_indexes,
            "aggregate_status": aggregate_status,
            "states": states,
        }

    def get_recent_delivery_logs(self, user_id: int, limit: int = 20):
        with self.lock:
            try:
                cursor = self.conn.cursor()
                safe_limit = max(1, min(int(limit), 200))
                cursor.execute(
                    """
                    SELECT id, user_id, account_id, order_id, item_id, buyer_id, buyer_nick,
                           rule_id, rule_keyword, card_type, match_mode, channel, status, reason, created_at
                    FROM delivery_logs
                    WHERE user_id = ?
                    ORDER BY datetime(created_at) DESC, id DESC
                    LIMIT ?
                    """,
                    (user_id, safe_limit),
                )
                return [self._delivery_log_row_to_dict(row) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"获取最近发货日志失败: user_id={user_id}, error={e}")
                raise

    def reserve_batch_data(self, card_id: int, order_id: str, unit_index: int = 1,
                           account_id: str = None, buyer_id: str = None, ttl_minutes: int = 30):
        normalized_account_id = self._require_account_id(account_id)
        normalized_unit_index = max(1, int(unit_index or 1))
        normalized_ttl_minutes = max(1, int(ttl_minutes or 30))

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT id, card_id, order_id, account_id, buyer_id, unit_index, reserved_content, status,
                           last_error, created_at, updated_at, sent_at, finalized_at, released_at, expires_at
                    FROM data_card_reservations
                    WHERE card_id = ? AND order_id = ? AND unit_index = ?
                      AND status IN ('reserved', 'sent', 'consumed')
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (card_id, order_id, normalized_unit_index),
                )
                existing = cursor.fetchone()
                if existing:
                    existing_account_id = str(existing[3] or "").strip()
                    if existing_account_id != normalized_account_id:
                        return None
                    return {
                        "id": existing[0],
                        "card_id": existing[1],
                        "order_id": existing[2],
                        "account_id": existing[3],
                        "buyer_id": existing[4],
                        "unit_index": existing[5],
                        "reserved_content": existing[6],
                        "status": existing[7],
                        "last_error": existing[8],
                        "created_at": existing[9],
                        "updated_at": existing[10],
                        "sent_at": existing[11],
                        "finalized_at": existing[12],
                        "released_at": existing[13],
                        "expires_at": existing[14],
                    }

                cursor.execute("SELECT data_content FROM cards WHERE id = ? AND type = 'data'", (card_id,))
                card_row = cursor.fetchone()
                if not card_row or not card_row[0]:
                    return None

                lines = [line.strip() for line in str(card_row[0]).split("\n") if line.strip()]
                if not lines:
                    return None

                reserved_content = lines.pop(0)
                remaining_content = "\n".join(lines)

                cursor.execute(
                    """
                    UPDATE cards
                    SET data_content = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (remaining_content, card_id),
                )

                cursor.execute(
                    """
                    INSERT INTO data_card_reservations (
                        card_id, order_id, account_id, buyer_id, unit_index, reserved_content, status,
                        created_at, updated_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, datetime('now', ?))
                    """,
                    (
                        card_id,
                        order_id,
                        normalized_account_id,
                        buyer_id,
                        normalized_unit_index,
                        reserved_content,
                        f"+{normalized_ttl_minutes} minutes",
                    ),
                )

                reservation_id = cursor.lastrowid
                self.conn.commit()
                return {
                    "id": reservation_id,
                    "card_id": card_id,
                    "order_id": order_id,
                    "account_id": normalized_account_id,
                    "buyer_id": buyer_id,
                    "unit_index": normalized_unit_index,
                    "reserved_content": reserved_content,
                    "status": "reserved",
                }
            except Exception as e:
                logger.error(
                    f"预占批量数据失败: card_id={card_id}, order_id={order_id}, account_id={account_id}, error={e}"
                )
                self.conn.rollback()
                return None

    def add_risk_control_log(self, account_id: str, event_type: str = 'slider_captcha',
                             event_description: str = None, processing_result: str = None,
                             processing_status: str = 'processing', error_message: str = None,
                             session_id: str = None, trigger_scene: str = None,
                             result_code: str = None, event_meta: Any = None,
                             duration_ms: Optional[int] = None):
        normalized_account_id = self._require_account_id(account_id)

        try:
            with self.lock:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO risk_control_logs (
                        account_id, event_type, session_id, trigger_scene, result_code, event_description,
                        event_meta, processing_result, processing_status, error_message, duration_ms,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (
                        normalized_account_id,
                        event_type,
                        session_id,
                        trigger_scene,
                        result_code,
                        event_description,
                        self._serialize_risk_control_event_meta(event_meta),
                        processing_result,
                        processing_status,
                        error_message,
                        int(duration_ms) if duration_ms is not None else None,
                    ),
                )
                self.conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"添加风控日志失败: account_id={account_id}, error={e}")
            self.conn.rollback()
            raise

    def get_risk_control_logs(self, account_id: str = None, processing_status: str = None,
                              event_type: str = None, trigger_scene: str = None,
                              session_id: str = None, result_code: str = None,
                              date_from: str = None, date_to: str = None,
                              limit: int = 100, offset: int = 0) -> List[Dict]:
        with self.lock:
            try:
                cursor = self.conn.cursor()

                query = """
                    SELECT r.id, r.account_id, r.event_type, r.session_id, r.trigger_scene, r.result_code,
                           r.event_description, r.event_meta, r.processing_result, r.processing_status,
                           r.error_message, r.duration_ms, r.created_at, r.updated_at, c.id
                    FROM risk_control_logs r
                    LEFT JOIN cookies c ON r.account_id = c.id
                """
                conditions: List[str] = []
                params: List[Any] = []

                if account_id is not None:
                    normalized_account_id = self._require_account_id(account_id)
                    conditions.append("r.account_id = ?")
                    params.append(normalized_account_id)

                filter_specs = [
                    ("r.processing_status", processing_status),
                    ("r.event_type", event_type),
                    ("r.trigger_scene", trigger_scene),
                    ("r.session_id", session_id),
                    ("r.result_code", result_code),
                ]
                for column_name, raw_value in filter_specs:
                    value = str(raw_value or "").strip()
                    if value:
                        conditions.append(f"{column_name} = ?")
                        params.append(value)

                normalized_from = self._normalize_risk_log_datetime_param(date_from, end_of_day=False)
                if normalized_from:
                    conditions.append("datetime(r.created_at) >= datetime(?)")
                    params.append(normalized_from)

                normalized_to = self._normalize_risk_log_datetime_param(date_to, end_of_day=True)
                if normalized_to:
                    conditions.append("datetime(r.created_at) <= datetime(?)")
                    params.append(normalized_to)

                if conditions:
                    query += " WHERE " + " AND ".join(conditions)

                safe_limit = max(1, min(int(limit), 500))
                safe_offset = max(0, int(offset))
                query += " ORDER BY datetime(COALESCE(r.updated_at, r.created_at)) DESC, r.id DESC LIMIT ? OFFSET ?"
                params.extend([safe_limit, safe_offset])

                cursor.execute(query, params)
                return [self._risk_control_row_to_dict(row) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"获取风控日志失败: account_id={account_id}, error={e}")
                raise

    def get_risk_control_logs_count(self, account_id: str = None, processing_status: str = None,
                                    event_type: str = None, trigger_scene: str = None,
                                    session_id: str = None, result_code: str = None,
                                    date_from: str = None, date_to: str = None) -> int:
        with self.lock:
            try:
                cursor = self.conn.cursor()
                query = "SELECT COUNT(*) FROM risk_control_logs"
                conditions: List[str] = []
                params: List[Any] = []

                if account_id is not None:
                    normalized_account_id = self._require_account_id(account_id)
                    conditions.append("account_id = ?")
                    params.append(normalized_account_id)

                filter_specs = [
                    ("processing_status", processing_status),
                    ("event_type", event_type),
                    ("trigger_scene", trigger_scene),
                    ("session_id", session_id),
                    ("result_code", result_code),
                ]
                for column_name, raw_value in filter_specs:
                    value = str(raw_value or "").strip()
                    if value:
                        conditions.append(f"{column_name} = ?")
                        params.append(value)

                normalized_from = self._normalize_risk_log_datetime_param(date_from, end_of_day=False)
                if normalized_from:
                    conditions.append("datetime(created_at) >= datetime(?)")
                    params.append(normalized_from)

                normalized_to = self._normalize_risk_log_datetime_param(date_to, end_of_day=True)
                if normalized_to:
                    conditions.append("datetime(created_at) <= datetime(?)")
                    params.append(normalized_to)

                if conditions:
                    query += " WHERE " + " AND ".join(conditions)

                cursor.execute(query, params)
                result = cursor.fetchone()
                return int(result[0] if result else 0)
            except Exception as e:
                logger.error(f"获取风控日志数量失败: account_id={account_id}, error={e}")
                raise

    def get_slider_verification_session_stats(self, account_ids: Optional[List[str]] = None, range_key: str = 'all') -> Dict[str, Any]:
        empty_stats = {
            "has_data": False,
            "total_sessions": 0,
            "total_attempts": 0,
            "success_count": 0,
            "failure_count": 0,
            "processing_count": 0,
            "completed_sessions": 0,
            "success_rate": 0.0,
            "recent_success": None,
            "recent_failure": None,
            "accounts_with_sessions": 0,
            "accounts_with_failures": 0,
            "stats_mode": "session",
            "summary_text": "暂无滑块验证记录",
            "selected_range": "all",
            "range_label": "所有",
        }

        def _normalize_account_ids(values: Optional[List[str]]) -> Optional[List[str]]:
            if values is None:
                return None
            normalized_values = []
            for value in values:
                text = str(value or "").strip()
                if text:
                    normalized_values.append(text)
            return normalized_values

        def _format_datetime_text(value: Any) -> Optional[str]:
            if not isinstance(value, str):
                return None
            text = value.strip()
            return text[:16] if text else None

        def _normalize_range(value: Any) -> str:
            text = str(value or "").strip().lower()
            return text if text in {"today", "7d", "all"} else "all"

        def _build_range_filter(value: str) -> Tuple[List[str], List[Any], str]:
            normalized_range = _normalize_range(value)
            label_map = {
                "today": "当日",
                "7d": "近 7 天",
                "all": "所有",
            }
            if normalized_range == "all":
                return [], [], label_map[normalized_range]

            beijing_tz = timezone(timedelta(hours=8))
            now_local = datetime.now(beijing_tz)
            days_back = 0 if normalized_range == "today" else 6
            start_local = (now_local - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
            start_utc = start_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            return ["datetime(created_at) >= datetime(?)"], [start_utc], label_map[normalized_range]

        normalized_account_ids = _normalize_account_ids(account_ids)
        normalized_range = _normalize_range(range_key)

        if account_ids is not None and not normalized_account_ids:
            empty_result = dict(empty_stats)
            empty_result.update({
                "selected_range": normalized_range,
                "range_label": _build_range_filter(normalized_range)[2],
            })
            return empty_result

        with self.lock:
            try:
                cursor = self.conn.cursor()
                scope_conditions: List[str] = []
                scope_params: List[Any] = []

                if normalized_account_ids is not None:
                    placeholders = ", ".join(["?"] * len(normalized_account_ids))
                    scope_conditions.append(f"account_id IN ({placeholders})")
                    scope_params.extend(normalized_account_ids)

                range_conditions, range_params, range_label = _build_range_filter(normalized_range)
                scope_conditions.extend(range_conditions)
                scope_params.extend(range_params)

                where_clause = ""
                if scope_conditions:
                    where_clause = " WHERE " + " AND ".join(scope_conditions)

                cursor.execute(
                    f"""
                    SELECT
                        COALESCE(SUM(CASE WHEN event_type = 'slider_captcha' AND processing_status = 'success' THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN ((event_type = 'slider_captcha' AND processing_status = 'failed') OR result_code = 'password_login_slider_failed') THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN event_type = 'slider_captcha' AND processing_status = 'processing' THEN 1 ELSE 0 END), 0),
                        COUNT(DISTINCT CASE WHEN (event_type = 'slider_captcha' OR result_code = 'password_login_slider_failed') THEN account_id END)
                    FROM risk_control_logs
                    {where_clause}
                    """,
                    scope_params,
                )
                row = cursor.fetchone() or (0, 0, 0, 0)

                success_count = int(row[0] or 0)
                failure_count = int(row[1] or 0)
                processing_count = int(row[2] or 0)
                accounts_with_sessions = int(row[3] or 0)
                completed_sessions = success_count + failure_count
                total_sessions = completed_sessions + processing_count
                success_rate = round((success_count / completed_sessions) * 100, 1) if completed_sessions > 0 else 0.0

                def _fetch_recent_datetime(extra_condition: str, extra_params: List[Any]) -> Optional[str]:
                    conditions = list(scope_conditions)
                    params = list(scope_params)
                    conditions.append(extra_condition)
                    params.extend(extra_params)
                    recent_where = " WHERE " + " AND ".join(conditions)

                    cursor.execute(
                        f"""
                        SELECT COALESCE(updated_at, created_at)
                        FROM risk_control_logs
                        {recent_where}
                        ORDER BY datetime(COALESCE(updated_at, created_at)) DESC, id DESC
                        LIMIT 1
                        """,
                        params,
                    )
                    recent_row = cursor.fetchone()
                    return _format_datetime_text(recent_row[0] if recent_row else None)

                if total_sessions > 0:
                    if normalized_range == "all":
                        summary_text = "已包含全部时间的滑块成功/失败统计"
                    else:
                        summary_text = f"已按{range_label}范围统计滑块成功/失败"
                else:
                    summary_text = "暂无滑块验证记录" if normalized_range == "all" else f"{range_label}暂无滑块验证记录"

                return {
                    "has_data": total_sessions > 0,
                    "total_sessions": total_sessions,
                    "total_attempts": total_sessions,
                    "success_count": success_count,
                    "failure_count": failure_count,
                    "processing_count": processing_count,
                    "completed_sessions": completed_sessions,
                    "success_rate": success_rate,
                    "recent_success": _fetch_recent_datetime("event_type = ? AND processing_status = ?", ["slider_captcha", "success"]),
                    "recent_failure": _fetch_recent_datetime(
                        "((event_type = ? AND processing_status = ?) OR result_code = ?)",
                        ["slider_captcha", "failed", "password_login_slider_failed"],
                    ),
                    "accounts_with_sessions": accounts_with_sessions,
                    "accounts_with_failures": accounts_with_sessions,
                    "stats_mode": "session",
                    "summary_text": summary_text,
                    "selected_range": normalized_range,
                    "range_label": range_label,
                }
            except Exception as e:
                logger.error(f"获取滑块验证统计失败: error={e}")
                raise

    def mark_stale_risk_control_logs_failed(self, timeout_minutes: int = 15, account_id: str = None) -> int:
        try:
            with self.lock:
                cursor = self.conn.cursor()
                normalized_account_id = self._require_account_id(account_id) if account_id is not None else None

                params: List[Any] = [
                    f"处理超时（{timeout_minutes}分钟），系统自动关闭",
                    "处理超时，自动标记失败",
                ]
                where_conditions = ["processing_status = 'processing'"]

                if normalized_account_id is not None:
                    where_conditions.append("account_id = ?")
                    params.append(normalized_account_id)

                where_conditions.append("datetime(created_at) <= datetime('now', '-' || ? || ' minutes')")
                params.append(int(timeout_minutes))

                cursor.execute(
                    f"""
                    UPDATE risk_control_logs
                    SET
                        processing_status = 'failed',
                        error_message = COALESCE(error_message, ?),
                        processing_result = COALESCE(processing_result, ?),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE {' AND '.join(where_conditions)}
                    """,
                    params,
                )

                self.conn.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error(f"标记超时风控日志失败: account_id={account_id}, error={e}")
            self.conn.rollback()
            raise

    def export_backup(self, user_id: int = None) -> Dict[str, any]:
        def _fetch_table(cursor, table_name: str, where_clause: str = "", params: List[Any] = None) -> Dict[str, Any]:
            params = params or []
            cursor.execute(f"SELECT * FROM {table_name}{where_clause}", params)
            columns = [description[0] for description in cursor.description]
            rows = cursor.fetchall()
            return {
                "columns": columns,
                "rows": [list(row) for row in rows],
            }

        with self.lock:
            try:
                cursor = self.conn.cursor()
                backup_data = {
                    "version": "1.0",
                    "timestamp": time.time(),
                    "user_id": user_id,
                    "data": {},
                }

                if user_id is not None:
                    backup_data["data"]["cookies"] = _fetch_table(cursor, "cookies", " WHERE user_id = ?", [user_id])
                    cookie_rows = backup_data["data"]["cookies"]["rows"]
                    user_account_ids = [row[0] for row in cookie_rows]

                    user_scoped_tables = (
                        "notification_templates",
                        "notification_channels",
                        "user_settings",
                        "cards",
                        "delivery_rules",
                        "ai_config_presets",
                    )
                    for table_name in user_scoped_tables:
                        try:
                            backup_data["data"][table_name] = _fetch_table(
                                cursor,
                                table_name,
                                " WHERE user_id = ?",
                                [user_id],
                            )
                        except Exception:
                            pass

                    if user_account_ids:
                        placeholders = ",".join(["?"] * len(user_account_ids))
                        where_clause = f" WHERE account_id IN ({placeholders})"
                        related_tables = [
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
                        ]

                        for table_name in related_tables:
                            backup_data["data"][table_name] = _fetch_table(
                                cursor,
                                table_name,
                                where_clause,
                                user_account_ids,
                            )
                else:
                    tables = [
                        "cookies",
                        "keywords",
                        "cookie_status",
                        "cards",
                        "delivery_rules",
                        "delivery_logs",
                        "delivery_finalization_states",
                        "data_card_reservations",
                        "default_replies",
                        "default_reply_records",
                        "notification_templates",
                        "notification_channels",
                        "message_notifications",
                        "user_settings",
                        "ai_config_presets",
                        "system_settings",
                        "item_info",
                        "item_replay",
                        "comment_templates",
                        "risk_control_logs",
                        "orders",
                        "scheduled_tasks",
                        "ai_reply_settings",
                        "ai_conversations",
                        "ai_item_cache",
                    ]

                    for table_name in tables:
                        backup_data["data"][table_name] = _fetch_table(cursor, table_name)

                logger.info(f"导出备份成功: user_id={user_id}")
                return backup_data
            except Exception as e:
                logger.error(f"导出备份失败: {e}")
                raise

    def import_backup(self, backup_data: Dict[str, any], user_id: int = None) -> bool:
        if not isinstance(backup_data, dict) or "data" not in backup_data:
            raise ValueError("备份数据格式无效")

        def _table_columns(cursor, table_name: str) -> List[str]:
            cursor.execute(f"PRAGMA table_info({table_name})")
            return [row[1] for row in cursor.fetchall()]

        def _all_user_ids(cursor) -> List[int]:
            cursor.execute("SELECT id FROM users ORDER BY id")
            return [int(row[0]) for row in cursor.fetchall() if row and row[0] is not None]

        user_scoped_autoincrement_tables = {
            "notification_templates",
            "notification_channels",
            "user_settings",
            "ai_config_presets",
            "cards",
            "delivery_rules",
            "default_reply_records",
            "message_notifications",
            "item_info",
            "item_replay",
            "comment_templates",
            "risk_control_logs",
            "scheduled_tasks",
            "delivery_logs",
            "delivery_finalization_states",
            "data_card_reservations",
            "ai_conversations",
        }
        remapped_identity_tables = {
            "notification_channels",
            "cards",
            "delivery_rules",
        }
        relation_id_maps: Dict[str, Dict[int, int]] = {
            table_name: {}
            for table_name in remapped_identity_tables
        }

        def _normalize_mapping_key(raw_value: Any) -> Optional[int]:
            if raw_value in (None, ""):
                return None
            try:
                return int(raw_value)
            except (TypeError, ValueError):
                return None

        def _resolve_remapped_id(table_name: str, raw_value: Any) -> Optional[int]:
            normalized_key = _normalize_mapping_key(raw_value)
            if normalized_key is None:
                return None
            return relation_id_maps.get(table_name, {}).get(normalized_key)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                self._execute_sql(cursor, "BEGIN TRANSACTION")

                if user_id is not None:
                    cursor.execute("SELECT id FROM cookies WHERE user_id = ?", (user_id,))
                    user_account_ids = [row[0] for row in cursor.fetchall()]

                    if user_account_ids:
                        placeholders = ",".join(["?"] * len(user_account_ids))
                        related_tables = [
                            "message_notifications",
                            "default_reply_records",
                            "comment_templates",
                            "default_replies",
                            "item_replay",
                            "item_info",
                            "cookie_status",
                            "keywords",
                            "risk_control_logs",
                            "ai_conversations",
                            "ai_reply_settings",
                            "orders",
                            "scheduled_tasks",
                            "delivery_logs",
                            "delivery_finalization_states",
                            "data_card_reservations",
                        ]
                        for table_name in related_tables:
                            cursor.execute(
                                f"DELETE FROM {table_name} WHERE account_id IN ({placeholders})",
                                user_account_ids,
                            )

                        cursor.execute("DELETE FROM cookies WHERE user_id = ?", (user_id,))

                    user_scoped_tables = (
                        "scheduled_tasks",
                        "delivery_rules",
                        "cards",
                        "ai_config_presets",
                        "user_settings",
                        "notification_channels",
                        "notification_templates",
                    )
                    for table_name in user_scoped_tables:
                        if table_name in backup_data.get("data", {}):
                            cursor.execute(f"DELETE FROM {table_name} WHERE user_id = ?", (user_id,))
                else:
                    delete_tables = [
                        "data_card_reservations",
                        "delivery_finalization_states",
                        "delivery_logs",
                        "message_notifications",
                        "default_reply_records",
                        "comment_templates",
                        "default_replies",
                        "risk_control_logs",
                        "ai_conversations",
                        "ai_reply_settings",
                        "orders",
                        "scheduled_tasks",
                        "notification_templates",
                        "item_replay",
                        "item_info",
                        "cookie_status",
                        "keywords",
                        "delivery_rules",
                        "cards",
                        "ai_config_presets",
                        "user_settings",
                        "notification_channels",
                        "ai_item_cache",
                        "cookies",
                    ]
                    for table_name in delete_tables:
                        cursor.execute(f"DELETE FROM {table_name}")

                    self._execute_sql(cursor, "DELETE FROM system_settings WHERE key != 'admin_password_hash'")

                import_order = [
                    "cookies",
                    "notification_templates",
                    "notification_channels",
                    "user_settings",
                    "ai_config_presets",
                    "cards",
                    "delivery_rules",
                    "system_settings",
                    "keywords",
                    "cookie_status",
                    "default_replies",
                    "default_reply_records",
                    "message_notifications",
                    "item_info",
                    "item_replay",
                    "comment_templates",
                    "risk_control_logs",
                    "orders",
                    "scheduled_tasks",
                    "delivery_logs",
                    "delivery_finalization_states",
                    "data_card_reservations",
                    "ai_reply_settings",
                    "ai_conversations",
                    "ai_item_cache",
                ]

                data = backup_data["data"]
                for table_name in import_order:
                    table_data = data.get(table_name)
                    if not table_data:
                        continue

                    columns = list(table_data.get("columns") or [])
                    rows = list(table_data.get("rows") or [])
                    if not columns or not rows:
                        continue

                    existing_columns = _table_columns(cursor, table_name)
                    filtered_columns = [column for column in columns if column in existing_columns]
                    if (
                        user_id is not None
                        and table_name in user_scoped_autoincrement_tables
                    ):
                        filtered_columns = [column for column in filtered_columns if column != "id"]
                    if (
                        table_name in {
                            "notification_templates",
                            "notification_channels",
                            "user_settings",
                            "cards",
                            "delivery_rules",
                            "ai_config_presets",
                            "scheduled_tasks",
                            "delivery_logs",
                        }
                        and "user_id" in existing_columns
                        and "user_id" not in filtered_columns
                    ):
                        filtered_columns.append("user_id")
                    if not filtered_columns:
                        continue

                    prepared_row_dicts = []
                    import_target_user_ids = None
                    for row in rows:
                        row_dict = dict(zip(columns, row))
                        if table_name == "notification_channels" and "type" in filtered_columns:
                            row_dict["type"] = self._normalize_notification_channel_type(row_dict.get("type"))
                        if user_id is not None and table_name in {
                            "cookies",
                            "notification_channels",
                            "notification_templates",
                            "user_settings",
                            "cards",
                            "delivery_rules",
                            "ai_config_presets",
                            "scheduled_tasks",
                            "delivery_logs",
                        }:
                            row_dict["user_id"] = user_id
                        if user_id is not None and table_name == "message_notifications":
                            mapped_channel_id = _resolve_remapped_id("notification_channels", row_dict.get("channel_id"))
                            if mapped_channel_id is None:
                                logger.warning(
                                    f"用户备份导入跳过消息通知记录：未找到通知渠道映射 channel_id={row_dict.get('channel_id')}"
                                )
                                continue
                            row_dict["channel_id"] = mapped_channel_id
                        if user_id is not None and table_name == "delivery_rules":
                            mapped_card_id = _resolve_remapped_id("cards", row_dict.get("card_id"))
                            if mapped_card_id is None:
                                logger.warning(
                                    f"用户备份导入跳过发货规则：未找到卡券映射 card_id={row_dict.get('card_id')}"
                                )
                                continue
                            row_dict["card_id"] = mapped_card_id
                        if user_id is not None and table_name == "delivery_logs":
                            mapped_rule_id = _resolve_remapped_id("delivery_rules", row_dict.get("rule_id"))
                            if row_dict.get("rule_id") not in (None, ""):
                                row_dict["rule_id"] = mapped_rule_id
                        if user_id is not None and table_name == "data_card_reservations":
                            mapped_card_id = _resolve_remapped_id("cards", row_dict.get("card_id"))
                            if mapped_card_id is None:
                                logger.warning(
                                    f"用户备份导入跳过批量数据预占记录：未找到卡券映射 card_id={row_dict.get('card_id')}"
                                )
                                continue
                            row_dict["card_id"] = mapped_card_id
                        if table_name == "notification_templates" and "user_id" in filtered_columns:
                            raw_template_user_id = row_dict.get("user_id")
                            target_user_ids = []
                            if user_id is not None:
                                target_user_ids = [user_id]
                            elif raw_template_user_id not in (None, ""):
                                try:
                                    target_user_ids = [int(raw_template_user_id)]
                                except (TypeError, ValueError):
                                    target_user_ids = []
                            else:
                                if import_target_user_ids is None:
                                    import_target_user_ids = _all_user_ids(cursor)
                                target_user_ids = list(import_target_user_ids)

                            for target_user_id in target_user_ids:
                                cloned_row_dict = dict(row_dict)
                                cloned_row_dict["user_id"] = target_user_id
                                prepared_row_dicts.append(cloned_row_dict)
                            continue

                        prepared_row_dicts.append(row_dict)

                    if not prepared_row_dicts:
                        continue

                    placeholders = ",".join(["?"] * len(filtered_columns))
                    if table_name == "system_settings":
                        for row_dict in prepared_row_dicts:
                            row_values = [row_dict.get(column) for column in filtered_columns]
                            key_value = row_values[filtered_columns.index("key")] if "key" in filtered_columns else None
                            if key_value != "admin_password_hash":
                                cursor.execute(
                                    f"INSERT INTO {table_name} ({','.join(filtered_columns)}) VALUES ({placeholders})",
                                    row_values,
                                )
                    elif user_id is not None and table_name in remapped_identity_tables:
                        for row_dict in prepared_row_dicts:
                            row_values = [row_dict.get(column) for column in filtered_columns]
                            cursor.execute(
                                f"INSERT INTO {table_name} ({','.join(filtered_columns)}) VALUES ({placeholders})",
                                row_values,
                            )
                            original_row_id = _normalize_mapping_key(row_dict.get("id"))
                            if original_row_id is not None:
                                relation_id_maps[table_name][original_row_id] = int(cursor.lastrowid)
                    else:
                        filtered_rows = [
                            [row_dict.get(column) for column in filtered_columns]
                            for row_dict in prepared_row_dicts
                        ]
                        cursor.executemany(
                            f"INSERT INTO {table_name} ({','.join(filtered_columns)}) VALUES ({placeholders})",
                            filtered_rows,
                        )

                if user_id is None:
                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO system_settings (key, value, description) VALUES
                        ('theme_color', '#4f46e5', '主题颜色'),
                        ('registration_enabled', 'true', '是否开启用户注册'),
                        ('show_default_login_info', 'true', '是否显示默认登录信息'),
                        ('login_captcha_enabled', 'true', '是否开启登录验证码'),
                        ('smtp_server', '', 'SMTP服务器地址'),
                        ('smtp_port', '587', 'SMTP端口'),
                        ('smtp_user', '', 'SMTP登录用户名（发件邮箱）'),
                        ('smtp_password', '', 'SMTP登录密码/授权码'),
                        ('smtp_from', '', '发件人显示名（留空则使用邮箱地址）'),
                        ('smtp_use_tls', 'true', '是否启用TLS'),
                        ('smtp_use_ssl', 'false', '是否启用SSL'),
                        ('verification_email_api_url', '', '验证码邮件API地址（留空则仅使用SMTP）'),
                        ('qq_notification_api_url', '', 'QQ通知API地址（留空则禁用QQ通知）'),
                        ('auto_comment_api_url', '', '自动好评辅助API地址（留空则禁用外部辅助）'),
                        ('qq_reply_secret_key', 'xianyu_qq_reply_2024', 'QQ回复消息API秘钥')
                        """
                    )

                self.conn.commit()
                logger.info(f"导入备份成功: user_id={user_id}")
                return True
            except Exception as e:
                logger.error(f"导入备份失败: {e}")
                self.conn.rollback()
                raise

    def save_cookie(self, account_id: str, cookie_value: str, user_id: int = None) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("SELECT user_id FROM cookies WHERE id = ?", (normalized_account_id,))
                existing_row = cursor.fetchone()

                if existing_row is not None and user_id is not None and existing_row[0] not in (None, user_id):
                    logger.warning(
                        f"账号 {normalized_account_id} 已绑定用户 {existing_row[0]}，拒绝迁移到用户 {user_id}"
                    )
                    return False

                encrypted_cookie_value = self._encrypt_secret(cookie_value)
                if existing_row is None:
                    cursor.execute(
                        "INSERT INTO cookies (id, value, user_id) VALUES (?, ?, ?)",
                        (normalized_account_id, encrypted_cookie_value, user_id),
                    )
                else:
                    cursor.execute(
                        "UPDATE cookies SET value = ?, user_id = ? WHERE id = ?",
                        (
                            encrypted_cookie_value,
                            user_id if user_id is not None else existing_row[0],
                            normalized_account_id,
                        ),
                    )

                self.conn.commit()
                return True
            except Exception as e:
                logger.error(f"保存账号 Cookie 失败: account_id={account_id}, error={e}")
                self.conn.rollback()
                raise

    def delete_cookie(self, account_id: str, user_id: int = None) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                if user_id is not None:
                    cursor.execute("SELECT user_id FROM cookies WHERE id = ?", (normalized_account_id,))
                    existing_row = cursor.fetchone()
                    if existing_row is not None and existing_row[0] not in (None, user_id):
                        logger.warning(
                            f"账号 {normalized_account_id} 已绑定用户 {existing_row[0]}，拒绝删除给用户 {user_id}"
                        )
                        return False
                cursor.execute('BEGIN TRANSACTION')
                self._delete_account_scoped_rows(cursor, [normalized_account_id])
                cursor.execute("DELETE FROM cookies WHERE id = ?", (normalized_account_id,))
                deleted = cursor.rowcount > 0
                cursor.execute('COMMIT')
                return deleted
            except Exception as e:
                logger.error(f"删除账号 Cookie 失败: account_id={account_id}, error={e}")
                self.conn.rollback()
                raise

    def get_cookie(self, account_id: str) -> Optional[str]:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("SELECT value FROM cookies WHERE id = ?", (normalized_account_id,))
                result = cursor.fetchone()
                if not result:
                    return None
                return self._decrypt_secret(result[0])
            except Exception as e:
                logger.error(f"获取账号 Cookie 失败: account_id={account_id}, error={e}")
                raise

    def get_cookie_by_id(self, account_id: str) -> Optional[Dict[str, str]]:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "SELECT id, value, created_at FROM cookies WHERE id = ?",
                    (normalized_account_id,),
                )
                result = cursor.fetchone()
                if not result:
                    return None

                cookie_value = self._decrypt_secret(result[1])
                return {
                    "id": result[0],
                    "account_id": result[0],
                    "cookies_str": cookie_value,
                    "value": cookie_value,
                    "created_at": result[2],
                }
            except Exception as e:
                logger.error(f"根据账号 ID 获取 Cookie 失败: account_id={account_id}, error={e}")
                raise

    def get_cookie_details(self, account_id: str) -> Optional[Dict[str, Any]]:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT id, value, user_id, auto_confirm, bound_unb, bind_status,
                           remark, pause_duration, username, password, show_browser,
                           created_at, proxy_type, proxy_host, proxy_port, proxy_user, proxy_pass
                    FROM cookies
                    WHERE id = ?
                    """,
                    (normalized_account_id,),
                )
                result = cursor.fetchone()
                if not result:
                    return None

                cookie_value = self._decrypt_secret(result[1])
                password = self._decrypt_secret(result[9])
                proxy_pass = self._decrypt_secret(result[16])
                return {
                    "id": result[0],
                    "account_id": result[0],
                    "value": cookie_value,
                    "cookie_value": cookie_value,
                    "user_id": result[2],
                    "auto_confirm": bool(result[3]),
                    "bound_unb": result[4] or "",
                    "bind_status": result[5] or "active",
                    "remark": result[6] or "",
                    "pause_duration": result[7] if result[7] is not None else 10,
                    "username": result[8] or "",
                    "password": password,
                    "show_browser": bool(result[10]) if result[10] is not None else False,
                    "created_at": result[11],
                    "proxy_type": result[12] or "none",
                    "proxy_host": result[13] or "",
                    "proxy_port": result[14] or 0,
                    "proxy_user": result[15] or "",
                    "proxy_pass": proxy_pass,
                }
            except Exception as e:
                logger.error(f"获取账号详细信息失败: account_id={account_id}, error={e}")
                raise

    def update_cookie_remark(self, account_id: str, remark: str) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "UPDATE cookies SET remark = ? WHERE id = ?",
                    (remark, normalized_account_id),
                )
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"更新账号备注失败: account_id={account_id}, error={e}")
                self.conn.rollback()
                raise

    def update_cookie_account_info(
        self,
        account_id: str,
        cookie_value: str = None,
        username: str = None,
        password: str = None,
        user_id: int = None,
    ) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("SELECT id, user_id FROM cookies WHERE id = ?", (normalized_account_id,))
                existing_row = cursor.fetchone()

                if existing_row is None:
                    if cookie_value is None:
                        logger.warning(
                            f"账号 {normalized_account_id} 不存在，且未提供 cookie_value，无法创建新记录"
                        )
                        return False

                    if user_id is None:
                        cursor.execute("SELECT id FROM users WHERE username = 'admin'")
                        admin_user = cursor.fetchone()
                        user_id = admin_user[0] if admin_user else 1

                    insert_fields = ["id", "value", "user_id"]
                    insert_values = [normalized_account_id, self._encrypt_secret(cookie_value), user_id]
                    placeholders = ["?", "?", "?"]

                    if username is not None:
                        insert_fields.append("username")
                        insert_values.append(username)
                        placeholders.append("?")

                    if password is not None:
                        insert_fields.append("password")
                        insert_values.append(self._encrypt_secret(password))
                        placeholders.append("?")

                    cursor.execute(
                        f"INSERT INTO cookies ({', '.join(insert_fields)}) VALUES ({', '.join(placeholders)})",
                        tuple(insert_values),
                    )
                    self.conn.commit()
                    return True

                existing_user_id = existing_row[1]
                if user_id is not None and user_id != existing_user_id:
                    logger.warning(
                        f"账号 {normalized_account_id} 已绑定用户 {existing_user_id}，拒绝迁移到用户 {user_id}"
                    )
                    return False

                update_fields = []
                params = []

                if cookie_value is not None:
                    update_fields.append("value = ?")
                    params.append(self._encrypt_secret(cookie_value))

                if username is not None:
                    update_fields.append("username = ?")
                    params.append(username)

                if password is not None:
                    update_fields.append("password = ?")
                    params.append(self._encrypt_secret(password))

                if not update_fields:
                    logger.warning(f"更新账号 {normalized_account_id} 信息时没有提供任何更新字段")
                    return False

                params.append(normalized_account_id)
                cursor.execute(
                    f"UPDATE cookies SET {', '.join(update_fields)} WHERE id = ?",
                    tuple(params),
                )
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"更新账号信息失败: account_id={account_id}, error={e}")
                self.conn.rollback()
                raise

    def update_cookie_proxy_config(
        self,
        account_id: str,
        proxy_type: str = None,
        proxy_host: str = None,
        proxy_port: int = None,
        proxy_user: str = None,
        proxy_pass: str = None,
    ) -> bool:
        normalized_account_id = self._require_account_id(account_id)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("SELECT 1 FROM cookies WHERE id = ?", (normalized_account_id,))
                if not cursor.fetchone():
                    return False

                update_fields = []
                params = []

                if proxy_type is not None:
                    update_fields.append("proxy_type = ?")
                    params.append(proxy_type)

                if proxy_host is not None:
                    update_fields.append("proxy_host = ?")
                    params.append(proxy_host)

                if proxy_port is not None:
                    update_fields.append("proxy_port = ?")
                    params.append(proxy_port)

                if proxy_user is not None:
                    update_fields.append("proxy_user = ?")
                    params.append(proxy_user)

                if proxy_pass is not None:
                    update_fields.append("proxy_pass = ?")
                    params.append(self._encrypt_secret(proxy_pass))

                if not update_fields:
                    logger.warning(f"更新账号 {normalized_account_id} 代理配置时没有提供任何更新字段")
                    return False

                params.append(normalized_account_id)
                cursor.execute(
                    f"UPDATE cookies SET {', '.join(update_fields)} WHERE id = ?",
                    tuple(params),
                )
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"更新代理配置失败: account_id={account_id}, error={e}")
                self.conn.rollback()
                raise

    def get_cookie_proxy_config(self, account_id: str) -> Dict[str, any]:
        normalized_account_id = self._require_account_id(account_id)
        default_config = {
            "proxy_type": "none",
            "proxy_host": "",
            "proxy_port": 0,
            "proxy_user": "",
            "proxy_pass": "",
        }

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT proxy_type, proxy_host, proxy_port, proxy_user, proxy_pass
                    FROM cookies
                    WHERE id = ?
                    """,
                    (normalized_account_id,),
                )
                result = cursor.fetchone()
                if not result:
                    return dict(default_config)

                return {
                    "proxy_type": result[0] or "none",
                    "proxy_host": result[1] or "",
                    "proxy_port": result[2] or 0,
                    "proxy_user": result[3] or "",
                    "proxy_pass": self._decrypt_secret(result[4]),
                }
            except Exception as e:
                logger.error(f"获取代理配置失败: account_id={account_id}, error={e}")
                raise

    def save_item_basic_info(self, account_id: str, item_id: str, item_title: str = None,
                             item_description: str = None, item_category: str = None,
                             item_price: str = None, item_detail: str = None) -> bool:
        normalized_account_id = self._require_account_id(account_id)
        normalized_item_id = str(item_id or "").strip()
        if not normalized_item_id:
            return False

        normalized_item_detail = item_detail if item_detail is not None else ""
        if normalized_item_detail is not None and not isinstance(normalized_item_detail, str):
            normalized_item_detail = json.dumps(normalized_item_detail, ensure_ascii=False)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO item_info (
                        account_id, item_id, item_title, item_description, item_category,
                        item_price, item_detail, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (
                        normalized_account_id,
                        normalized_item_id,
                        item_title or "",
                        item_description or "",
                        item_category or "",
                        item_price or "",
                        normalized_item_detail or "",
                    ),
                )

                if cursor.rowcount > 0:
                    self.conn.commit()
                    return True

                update_parts = []
                params = []

                if item_title:
                    update_parts.append("item_title = CASE WHEN (item_title IS NULL OR item_title = '') THEN ? ELSE item_title END")
                    params.append(item_title)

                if item_description:
                    update_parts.append("item_description = CASE WHEN (item_description IS NULL OR item_description = '') THEN ? ELSE item_description END")
                    params.append(item_description)

                if item_category:
                    update_parts.append("item_category = CASE WHEN (item_category IS NULL OR item_category = '') THEN ? ELSE item_category END")
                    params.append(item_category)

                if item_price:
                    update_parts.append("item_price = CASE WHEN (item_price IS NULL OR item_price = '') THEN ? ELSE item_price END")
                    params.append(item_price)

                if normalized_item_detail:
                    update_parts.append("item_detail = CASE WHEN (item_detail IS NULL OR item_detail = '' OR TRIM(item_detail) = '') THEN ? ELSE item_detail END")
                    params.append(normalized_item_detail)

                if update_parts:
                    update_parts.append("updated_at = CURRENT_TIMESTAMP")
                    params.extend([normalized_account_id, normalized_item_id])
                    cursor.execute(
                        f"UPDATE item_info SET {', '.join(update_parts)} WHERE account_id = ? AND item_id = ?",
                        tuple(params),
                    )

                self.conn.commit()
                return True
            except Exception as e:
                logger.error(f"保存商品基本信息失败: account_id={account_id}, item_id={item_id}, error={e}")
                self.conn.rollback()
                return False

    def update_item_multi_spec_status(self, account_id: str, item_id: str, is_multi_spec: bool) -> bool:
        normalized_account_id = self._require_account_id(account_id)
        normalized_item_id = str(item_id or "").strip()
        if not normalized_item_id:
            return False

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    UPDATE item_info
                    SET is_multi_spec = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE account_id = ? AND item_id = ?
                    """,
                    (1 if is_multi_spec else 0, normalized_account_id, normalized_item_id),
                )
                if cursor.rowcount > 0:
                    self.conn.commit()
                    return True
                self.conn.rollback()
                return False
            except Exception as e:
                logger.error(f"更新商品多规格状态失败: account_id={account_id}, item_id={item_id}, error={e}")
                self.conn.rollback()
                raise

    def get_item_multi_spec_status(self, account_id: str, item_id: str) -> bool:
        normalized_account_id = self._require_account_id(account_id)
        normalized_item_id = str(item_id or "").strip()
        if not normalized_item_id:
            return False

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT is_multi_spec
                    FROM item_info
                    WHERE account_id = ? AND item_id = ?
                    """,
                    (normalized_account_id, normalized_item_id),
                )
                row = cursor.fetchone()
                return bool(row[0]) if row and row[0] is not None else False
            except Exception as e:
                logger.error(f"获取商品多规格状态失败: account_id={account_id}, item_id={item_id}, error={e}")
                raise

    def update_item_multi_quantity_delivery_status(self, account_id: str, item_id: str, multi_quantity_delivery: bool) -> bool:
        normalized_account_id = self._require_account_id(account_id)
        normalized_item_id = str(item_id or "").strip()
        if not normalized_item_id:
            return False

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    UPDATE item_info
                    SET multi_quantity_delivery = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE account_id = ? AND item_id = ?
                    """,
                    (1 if multi_quantity_delivery else 0, normalized_account_id, normalized_item_id),
                )
                if cursor.rowcount > 0:
                    self.conn.commit()
                    return True
                self.conn.rollback()
                return False
            except Exception as e:
                logger.error(f"更新商品多数量发货状态失败: account_id={account_id}, item_id={item_id}, error={e}")
                self.conn.rollback()
                raise

    def get_item_multi_quantity_delivery_status(self, account_id: str, item_id: str) -> bool:
        normalized_account_id = self._require_account_id(account_id)
        normalized_item_id = str(item_id or "").strip()
        if not normalized_item_id:
            return False

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT multi_quantity_delivery
                    FROM item_info
                    WHERE account_id = ? AND item_id = ?
                    """,
                    (normalized_account_id, normalized_item_id),
                )
                row = cursor.fetchone()
                return bool(row[0]) if row and row[0] is not None else False
            except Exception as e:
                logger.error(f"获取商品多数量发货状态失败: account_id={account_id}, item_id={item_id}, error={e}")
                raise

    def update_item_detail(self, account_id: str, item_id: str, item_detail: str) -> bool:
        normalized_account_id = self._require_account_id(account_id)
        normalized_item_id = str(item_id or "").strip()
        if not normalized_item_id:
            return False

        normalized_item_detail = item_detail
        if normalized_item_detail is not None and not isinstance(normalized_item_detail, str):
            normalized_item_detail = json.dumps(normalized_item_detail, ensure_ascii=False)

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    UPDATE item_info
                    SET item_detail = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE account_id = ? AND item_id = ?
                    """,
                    (normalized_item_detail, normalized_account_id, normalized_item_id),
                )
                if cursor.rowcount > 0:
                    self.conn.commit()
                    return True
                self.conn.rollback()
                return False
            except Exception as e:
                logger.error(f"更新商品详情失败: account_id={account_id}, item_id={item_id}, error={e}")
                self.conn.rollback()
                raise

    def update_item_title_only(self, account_id: str, item_id: str, item_title: str) -> bool:
        normalized_account_id = self._require_account_id(account_id)
        normalized_item_id = str(item_id or "").strip()
        if not normalized_item_id:
            return False

        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO item_info (
                        account_id, item_id, item_title, item_description,
                        item_category, item_price, item_detail, created_at, updated_at
                    ) VALUES (?, ?, ?, '', '', '', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(account_id, item_id) DO UPDATE SET
                        item_title = excluded.item_title,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (normalized_account_id, normalized_item_id, item_title or ""),
                )
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"更新商品标题失败: account_id={account_id}, item_id={item_id}, error={e}")
                self.conn.rollback()
                return False

db_manager = DBManager()

# 确保进程结束时关闭数据库连接
import atexit
atexit.register(db_manager.close)
