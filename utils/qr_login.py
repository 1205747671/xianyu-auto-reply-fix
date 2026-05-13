#!/usr/bin/env python3
"""
闲鱼扫码登录工具
基于API接口实现二维码生成和Cookie获取（参照myfish-main项目）
"""

import asyncio
import time
import uuid
import json
import re
import os
from random import random
from typing import Optional, Dict, Any
import httpx
import qrcode
import qrcode.constants
from loguru import logger
import hashlib
from urllib.parse import urlparse

from utils.account_browser_runtime import account_browser_runtime_manager
from utils.image_utils import image_manager


QR_CROSS_DOMAIN_COOKIE_NAMES = {
    "t",
    "tracknick",
    "isg",
    "unb",
    "cookie2",
    "_tb_token_",
    "sgcookie",
    "csg",
    "tfstk",
    "_m_h5_tk",
    "_m_h5_tk_enc",
    "havana_lgc2_77",
    "_hvn_lgc_",
    "havana_lgc_exp",
    "mtop_partitioned_detect",
    "_samesite_flag_",
    "sdkSilent",
    "cna",
    "x5sec",
    "x5secdata",
    "XSRF-TOKEN",
    "thw",
    "cbc",
    "cnaui",
    "aui",
    "sca",
}


def generate_headers():
    """生成请求头"""
    return {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
        'Referer': 'https://passport.goofish.com/',
        'Origin': 'https://passport.goofish.com',
    }


class GetLoginParamsError(Exception):
    """获取登录参数错误"""


class GetLoginQRCodeError(Exception):
    """获取登录二维码失败"""


class NotLoginError(Exception):
    """未登录错误"""


class QRLoginSession:
    """二维码登录会话"""

    def __init__(self, session_id: str, user_id: Optional[int] = None, account_id: Optional[str] = None):
        self.session_id = session_id
        self.user_id = user_id
        self.account_id = str(account_id or '').strip()
        self.status = 'waiting'  # waiting, scanned, confirmed, success, failed, expired, cancelled, verification_required
        self.phase = 'waiting_for_scan'
        self.verification_type = None
        self.error_message = None
        self.handoff_status = 'idle'
        self.handoff_error = None
        self.success_stage = None
        self.browser_alive = False
        self.account_info = None
        self.qr_code_url = None
        self.qr_content = None
        self.cookies = {}
        self.unb = None
        self.created_time = time.time()
        self.expire_time = 300  # 5分钟过期
        self.params = {}  # 存储登录参数
        self.verification_url = None  # 风控验证URL
        self.screenshot_path = None  # 风控验证截图
        self.verification_task = None  # 风控验证页面保持任务
        self.success_source = None  # 登录成功来源: api/browser
        self.managed_runtime_lease = None
        self.managed_runtime = None
        self.managed_context = None
        self.managed_page = None
        self.last_active_probe_time = 0.0
        self.proxy_config = {}
        self.proxy_url = None
        self.proxy_account_id = None

    def is_expired(self) -> bool:
        """检查是否过期"""
        return time.time() - self.created_time > self.expire_time

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'session_id': self.session_id,
            'user_id': self.user_id,
            'account_id': self.account_id,
            'status': self.status,
            'phase': self.phase,
            'verification_type': self.verification_type,
            'error_message': self.error_message,
            'handoff_status': self.handoff_status,
            'handoff_error': self.handoff_error,
            'success_stage': self.success_stage,
            'browser_alive': self.browser_alive,
            'qr_code_url': self.qr_code_url,
            'created_time': self.created_time,
            'is_expired': self.is_expired()
        }


class QRLoginManager:
    """二维码登录管理器"""

    def __init__(self):
        self.sessions: Dict[str, QRLoginSession] = {}
        self.headers = generate_headers()
        self.host = "https://passport.goofish.com"
        self.api_mini_login = f"{self.host}/mini_login.htm"
        self.api_generate_qr = f"{self.host}/newlogin/qrcode/generate.do"
        self.api_scan_status = f"{self.host}/newlogin/qrcode/query.do"
        self.api_h5_tk = "https://h5api.m.goofish.com/h5/mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get/1.0/"
        
        # 配置代理（如果需要的话，取消注释并修改代理地址）
        # self.proxy = "http://127.0.0.1:7890"
        self.proxy = None
        
        # 配置超时时间
        self.timeout = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=60.0)

    def _cookie_marshal(self, cookies: dict) -> str:
        """将Cookie字典转换为字符串"""
        return "; ".join([f"{k}={v}" for k, v in cookies.items()])

    def _build_browser_cookies(self, target_url: str, cookies: Dict[str, str]) -> list[Dict[str, Any]]:
        """兼容旧调用，统一走跨域 Cookie 注入格式。"""
        return self._build_cross_domain_browser_cookies(target_url, cookies)

    def _build_cross_domain_browser_cookies(self, target_url: str, cookies: Dict[str, str]) -> list[Dict[str, Any]]:
        """把 API 会话中的 Cookie 转成浏览器可用的跨域注入格式。"""
        browser_cookies = []
        parsed = urlparse(target_url or self.host)
        target_host = str(parsed.netloc or 'passport.goofish.com').strip().lower()
        target_domains = ['.goofish.com']
        if target_host.endswith('taobao.com'):
            target_domains = ['.taobao.com']

        seen = set()
        for name, value in (cookies or {}).items():
            if not name or value is None:
                continue

            domains = list(target_domains)
            if name in QR_CROSS_DOMAIN_COOKIE_NAMES:
                for cross_domain in ('.goofish.com', '.taobao.com'):
                    if cross_domain not in domains:
                        domains.append(cross_domain)

            for domain in domains:
                dedupe_key = (str(name), str(value), domain)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                browser_cookies.append({
                    'name': str(name),
                    'value': str(value),
                    'domain': domain,
                    'path': '/',
                })

        return browser_cookies

    def _build_proxy_url(self, proxy_config: Optional[Dict[str, Any]]) -> Optional[str]:
        normalized_proxy = dict(proxy_config or {})
        proxy_type = str(normalized_proxy.get('proxy_type') or '').strip().lower()
        proxy_host = str(normalized_proxy.get('proxy_host') or '').strip()
        proxy_port = normalized_proxy.get('proxy_port')
        if proxy_type in {'', 'none'} or not proxy_host or not proxy_port:
            return None

        auth = ''
        proxy_user = str(normalized_proxy.get('proxy_user') or '').strip()
        proxy_pass = str(normalized_proxy.get('proxy_pass') or '').strip()
        if proxy_user:
            auth = proxy_user
            if proxy_pass:
                auth += f":{proxy_pass}"
            auth += '@'

        return f"{proxy_type}://{auth}{proxy_host}:{proxy_port}"

    def _build_browser_proxy_settings(self, proxy_config: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        normalized_proxy = dict(proxy_config or {})
        proxy_type = str(normalized_proxy.get('proxy_type') or '').strip().lower()
        proxy_host = str(normalized_proxy.get('proxy_host') or '').strip()
        proxy_port = normalized_proxy.get('proxy_port')
        if proxy_type in {'', 'none'} or not proxy_host or not proxy_port:
            return None

        proxy_settings: Dict[str, str] = {
            'server': f"{proxy_type}://{proxy_host}:{proxy_port}",
        }
        proxy_user = str(normalized_proxy.get('proxy_user') or '').strip()
        proxy_pass = str(normalized_proxy.get('proxy_pass') or '').strip()
        if proxy_user:
            proxy_settings['username'] = proxy_user
        if proxy_pass:
            proxy_settings['password'] = proxy_pass
        return proxy_settings

    def _resolve_existing_account_proxy_config(self, session: QRLoginSession) -> Dict[str, Any]:
        if not session:
            return {}

        if session.proxy_config:
            return dict(session.proxy_config)

        if session.user_id and session.account_id:
            try:
                from db_manager import db_manager

                proxy_config = dict(db_manager.get_cookie_proxy_config(session.account_id) or {})
                session.proxy_config = proxy_config
                session.proxy_account_id = session.account_id
                if not self._build_proxy_url(proxy_config):
                    logger.info(f"扫码登录验证链路未匹配到可用代理，沿用直连: {session.account_id}")
                else:
                    logger.info(f"扫码登录验证链路复用账号代理: {session.account_id}")
                return proxy_config
            except Exception as e:
                logger.warning(f"扫码登录验证链路读取账号代理失败，沿用直连: {session.account_id}, 错误: {e}")
                return {}

        if not session.user_id or not session.unb:
            return {}

        try:
            from db_manager import db_manager
            from utils.xianyu_utils import trans_cookies

            existing_cookies = db_manager.get_all_cookies(session.user_id) or {}
            matched_account_id = None
            for account_id, cookie_value in existing_cookies.items():
                try:
                    existing_cookie_dict = trans_cookies(cookie_value)
                except Exception:
                    continue
                if str(existing_cookie_dict.get('unb') or '').strip() == str(session.unb or '').strip():
                    matched_account_id = account_id
                    break

            if not matched_account_id:
                return {}

            proxy_config = dict(db_manager.get_cookie_proxy_config(matched_account_id) or {})
            session.proxy_account_id = matched_account_id
            proxy_type = str(proxy_config.get('proxy_type') or '').strip().lower()
            if proxy_type in {'', 'none'}:
                logger.info(f"扫码登录验证链路未匹配到可用代理，沿用直连: {matched_account_id}")
                return {}

            session.proxy_config = proxy_config
            session.proxy_url = self._build_proxy_url(proxy_config)
            logger.info(
                f"扫码登录验证链路复用账号代理: {matched_account_id}, "
                f"server: {proxy_type}://{proxy_config.get('proxy_host')}:{proxy_config.get('proxy_port')}"
            )
            return dict(proxy_config)
        except Exception as proxy_error:
            logger.warning(f"解析扫码登录验证链路代理配置失败，忽略代理复用: {proxy_error}")
            return {}

    def _resolve_session_proxy_url(self, session: Optional[QRLoginSession]) -> Optional[str]:
        if session and session.proxy_url:
            return session.proxy_url

        if session:
            self._resolve_existing_account_proxy_config(session)
            if session.proxy_url:
                return session.proxy_url

        return self.proxy

    def _resolve_verification_proxy_settings(self, session: QRLoginSession) -> Optional[Dict[str, str]]:
        proxy_config = self._resolve_existing_account_proxy_config(session)
        return self._build_browser_proxy_settings(proxy_config)

    def _build_verification_browser_launch_args(self) -> list[str]:
        return [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--no-first-run',
            '--mute-audio',
            '--no-default-browser-check',
            '--force-color-profile=srgb',
            '--password-store=basic',
            '--use-mock-keychain',
            '--window-size=1600,900',
        ]

    def _should_show_verification_browser(self) -> bool:
        override = str(os.getenv("XY_QR_LOGIN_SHOW_BROWSER") or "").strip().lower()
        if override:
            return override in {"1", "true", "yes", "on"}
        docker_env = str(os.getenv("DOCKER_ENV") or "").strip().lower() in {"1", "true", "yes", "on"}
        return os.name == 'nt' and not docker_env

    def _build_verification_context_options(self, show_browser: bool) -> Dict[str, Any]:
        context_options: Dict[str, Any] = {
            'locale': 'zh-CN',
            'timezone': 'Asia/Shanghai',
            'color_scheme': 'light',
            'accept_downloads': True,
            'ignore_https_errors': True,
        }
        if show_browser:
            context_options['no_viewport'] = True
        else:
            context_options['viewport'] = {'width': 1600, 'height': 900}
        return context_options

    def _resolve_verification_profile_dir(self, session: QRLoginSession) -> str:
        account_id = str(getattr(session, 'account_id', '') or '').strip()
        if not account_id:
            raise ValueError("扫码登录验证会话缺少account_id，拒绝按unb/session_id派生profile")
        return account_browser_runtime_manager.resolve_profile_dir(account_id)

    async def _launch_verification_browser_context(self, session: QRLoginSession):
        show_browser = self._should_show_verification_browser()
        launch_options = {
            'headless': not show_browser,
            'args': self._build_verification_browser_launch_args(),
            'humanize': True,
            'human_preset': 'careful',
        }
        proxy_settings = self._resolve_verification_proxy_settings(session)
        if proxy_settings:
            launch_options['proxy'] = proxy_settings
        context_options = self._build_verification_context_options(show_browser)
        profile_dir = self._resolve_verification_profile_dir(session)
        runtime_request = {
            'account_id': session.account_id,
            'purpose': 'qr_login_verification',
            'profile_dir': profile_dir,
            'use_persistent_context': True,
            'launch_options': launch_options,
            'persistent_context_options': context_options,
        }

        try:
            lease = await account_browser_runtime_manager.acquire_runtime(
                session.account_id,
                'qr_login_verification',
                exclusive=True,
                runtime_request=runtime_request,
            )
            runtime = getattr(lease, 'runtime', None)
            context = getattr(runtime, 'context', None)
            browser = getattr(context, 'browser', None) or getattr(runtime, 'browser', None)
            if context is None:
                await account_browser_runtime_manager.release_runtime(
                    lease,
                    reason='verification_context_missing',
                )
                raise RuntimeError(f"扫码登录验证页 runtime 缺少 context: {session.session_id}")
            logger.info(
                f"扫码登录验证页复用 CloakBrowser 持久化画像: {session.session_id}, "
                f"profile_dir: {profile_dir}, headless: {not show_browser}"
            )
            return lease, browser, context, show_browser
        except Exception as persistent_error:
            logger.error(
                f"扫码登录持久化画像启动失败，拒绝降级到匿名上下文: {session.session_id}, "
                f"错误: {persistent_error}"
            )
            raise

    @staticmethod
    def _track_runtime_lease_page(lease: Any, page: Any) -> None:
        if lease is None or page is None:
            return
        lease_pages = getattr(lease, 'pages', None)
        if not isinstance(lease_pages, list):
            return
        if page not in lease_pages:
            lease_pages.append(page)

    @staticmethod
    def _untrack_runtime_lease_page(lease: Any, page: Any) -> None:
        if lease is None or page is None:
            return
        lease_pages = getattr(lease, 'pages', None)
        if not isinstance(lease_pages, list):
            return
        while page in lease_pages:
            lease_pages.remove(page)

    async def _get_or_create_context_page(self, context):
        context_pages = getattr(context, 'pages', None)
        if isinstance(context_pages, (list, tuple)):
            for existing_page in context_pages:
                if existing_page is None:
                    continue
                try:
                    if not existing_page.is_closed():
                        return existing_page
                except Exception:
                    continue
        return await context.new_page()

    def _normalize_cookie_dict(self, cookies: Any) -> Dict[str, str]:
        """将不同形式的Cookie数据统一转换为字典"""
        if isinstance(cookies, dict) or hasattr(cookies, 'items'):
            return {
                str(name): str(value)
                for name, value in cookies.items()
                if name and value is not None
            }

        normalized = {}
        for cookie in cookies or []:
            if not isinstance(cookie, dict):
                continue
            name = cookie.get('name')
            value = cookie.get('value')
            if name and value is not None:
                normalized[str(name)] = str(value)
        return normalized

    def _merge_session_cookies(self, session: QRLoginSession, cookies: Any):
        """合并Cookie到会话中"""
        cookie_dict = self._normalize_cookie_dict(cookies)
        if not cookie_dict:
            return

        session.cookies.update(cookie_dict)
        if cookie_dict.get('unb'):
            session.unb = cookie_dict['unb']

    def _update_session_state(
        self,
        session: Optional[QRLoginSession],
        *,
        status: Optional[str] = None,
        phase: Optional[str] = None,
        verification_type: Optional[str] = None,
        error_message: Optional[str] = None,
        handoff_status: Optional[str] = None,
        handoff_error: Optional[str] = None,
        success_stage: Optional[str] = None,
        browser_alive: Optional[bool] = None,
        verification_url: Optional[str] = None,
        clear_error: bool = False,
        clear_handoff_error: bool = False,
    ) -> Optional[QRLoginSession]:
        if not session:
            return None

        if status is not None:
            session.status = status
        if phase is not None:
            session.phase = phase
        if verification_type is not None:
            session.verification_type = verification_type
        if clear_error:
            session.error_message = None
        if error_message is not None:
            session.error_message = str(error_message)
        if handoff_status is not None:
            session.handoff_status = handoff_status
        if clear_handoff_error:
            session.handoff_error = None
        if handoff_error is not None:
            session.handoff_error = str(handoff_error)
        if success_stage is not None:
            session.success_stage = success_stage
        if browser_alive is not None:
            session.browser_alive = bool(browser_alive)
        if verification_url is not None:
            session.verification_url = verification_url
        return session

    def update_session_fields(self, session_id: str, **fields) -> Optional[QRLoginSession]:
        return self._update_session_state(self.sessions.get(session_id), **fields)

    def update_session_handoff_status(
        self,
        session_id: str,
        status: str,
        *,
        error: Optional[str] = None,
        account_info: Optional[Dict[str, Any]] = None,
        terminal: bool = True,
    ) -> Optional[QRLoginSession]:
        session = self.sessions.get(session_id)
        if not session:
            return None

        normalized_status = str(status or 'idle').strip() or 'idle'
        phase_map = {
            'idle': 'login_complete',
            'processing': 'handoff_processing',
            'success': 'handoff_completed',
            'failed': 'handoff_failed',
        }
        self._update_session_state(
            session,
            status=('failed' if normalized_status == 'failed' and terminal else session.status),
            handoff_status=normalized_status,
            handoff_error=(error if normalized_status == 'failed' else None),
            phase=phase_map.get(normalized_status, session.phase),
            error_message=(error if normalized_status == 'failed' and terminal else session.error_message),
            clear_error=(normalized_status != 'failed'),
            clear_handoff_error=normalized_status != 'failed',
        )
        if account_info is not None:
            session.account_info = account_info
        return session

    def _detect_verification_type(self, verification_url: Optional[str]) -> Optional[str]:
        url = str(verification_url or '').lower()
        if not url:
            return None
        if '/iv/' in url or 'face' in url:
            return 'face_verify'
        if 'qrcode' in url or 'scan' in url or 'qr' in url:
            return 'qr_verify'
        return 'identity_verify'

    def _handle_confirmed_qr_response(self, session: QRLoginSession, resp: httpx.Response) -> bool:
        payload = (
            resp.json()
            .get("content", {})
            .get("data", {})
        )
        self._merge_session_cookies(session, resp.cookies)

        if payload.get("iframeRedirect") is True:
            iframe_url = payload.get("iframeRedirectUrl")
            verification_type = self._detect_verification_type(iframe_url)
            self._update_session_state(
                session,
                status='verification_required',
                phase='awaiting_verification',
                verification_type=verification_type,
                error_message='扫码已确认，等待完成验证',
                success_stage='confirmed_requires_verification',
                browser_alive=False,
                verification_url=iframe_url,
                clear_handoff_error=True,
            )
            session.expire_time = max(session.expire_time, 600)
            self._resolve_existing_account_proxy_config(session)
            self._ensure_verification_task(session)
            logger.warning(f"账号被风控，需要继续验证: {session.session_id}, URL: {iframe_url}")
            return False

        if self._mark_session_success(session, resp.cookies, 'api', require_complete_cookies=True):
            self._update_session_state(
                session,
                phase='login_complete',
                success_stage='api_complete',
                browser_alive=False,
                clear_error=True,
                clear_handoff_error=True,
            )
            return True

        self._update_session_state(
            session,
            status='confirmed',
            phase='awaiting_complete_cookies',
            error_message='扫码已确认，正在等待完整登录Cookie',
            success_stage='confirmed_pending_cookies',
        )
        logger.warning(f"扫码登录API返回CONFIRMED，但关键Cookie仍不完整: {session.session_id}")
        return False

    def _has_completed_login_cookies(self, cookie_dict: Dict[str, str]) -> bool:
        """基于关键Cookie判断是否已经完成登录"""
        if not cookie_dict.get('unb'):
            return False

        companion_keys = ('cookie2', 'havana_lgc2_77', '_tb_token_', 'sgcookie')
        return any(cookie_dict.get(key) for key in companion_keys)

    def _is_logged_in_url(self, url: str) -> bool:
        """判断URL是否已经跳转到登录后的页面"""
        current_url = str(url or '')
        if not current_url:
            return False

        if 'www.goofish.com/im' in current_url:
            return True

        return (
            'goofish.com' in current_url and
            'passport.goofish.com' not in current_url and
            'mini_login' not in current_url and
            '/iv/' not in current_url
        )

    def _mark_session_success(
        self,
        session: QRLoginSession,
        cookies: Any,
        source: str,
        require_complete_cookies: bool = False,
        managed_runtime_lease=None,
        managed_runtime=None,
        managed_context=None,
        managed_page=None,
    ) -> bool:
        """统一的会话成功收口，避免多条链路重复覆盖状态"""
        if not session:
            return False

        self._merge_session_cookies(session, cookies)

        has_success_cookie = bool(session.cookies.get('unb'))
        has_complete_cookies = self._has_completed_login_cookies(session.cookies)
        if not has_success_cookie:
            return False
        if require_complete_cookies and not has_complete_cookies:
            return False

        was_success = session.status == 'success'
        session.status = 'success'
        session.success_source = session.success_source or source
        if managed_runtime_lease is not None:
            session.managed_runtime_lease = managed_runtime_lease
        if managed_runtime is not None:
            session.managed_runtime = managed_runtime
        if managed_context is not None:
            session.managed_context = managed_context
        if managed_page is not None:
            session.managed_page = managed_page
        if managed_runtime_lease is not None and managed_page is not None:
            self._track_runtime_lease_page(managed_runtime_lease, managed_page)
        self._update_session_state(
            session,
            phase='login_complete',
            success_stage=f'{source}_complete',
            clear_error=True,
            clear_handoff_error=True,
        )

        if not was_success:
            logger.info(
                f"扫码登录成功（来源: {source}）: {session.session_id}, "
                f"UNB: {session.unb}"
            )

        return True

    async def _context_cookie_dict(self, context) -> Dict[str, str]:
        """提取浏览器上下文中的Cookie字典"""
        cookies = await context.cookies()
        return self._normalize_cookie_dict(cookies)

    async def _probe_browser_login_success(
        self,
        session: QRLoginSession,
        page,
        context,
        managed_runtime=None,
        managed_runtime_lease=None,
    ) -> bool:
        """在浏览器侧兜底判断验证是否已经完成"""
        runtime = managed_runtime if managed_runtime is not None else getattr(context, 'browser', None)
        current_url = page.url
        cookie_dict = await self._context_cookie_dict(context)
        cookies_ready = self._has_completed_login_cookies(cookie_dict)
        url_ready = self._is_logged_in_url(current_url)

        if cookies_ready and url_ready:
            logger.info(
                f"扫码登录浏览器侧检测成功（当前页）: {session.session_id}, URL: {current_url}"
            )
            return self._mark_session_success(
                session,
                cookie_dict,
                'browser',
                require_complete_cookies=True,
                managed_runtime_lease=managed_runtime_lease,
                managed_runtime=runtime,
                managed_context=context,
                managed_page=page,
            )

        if not cookies_ready:
            return False

        context_pages = getattr(context, 'pages', None)
        if callable(context_pages):
            try:
                context_pages = context_pages()
            except Exception:
                context_pages = None

        if isinstance(context_pages, (list, tuple)):
            for existing_page in context_pages:
                if existing_page is None or existing_page is page:
                    continue
                existing_url = getattr(existing_page, 'url', '')
                if not self._is_logged_in_url(existing_url):
                    continue

                probe_cookie_dict = await self._context_cookie_dict(context)
                logger.info(
                    f"扫码登录浏览器侧复用现有页面确认成功: {session.session_id}, "
                    f"page_url: {existing_url}"
                )
                return self._mark_session_success(
                    session,
                    probe_cookie_dict,
                    'browser',
                    require_complete_cookies=True,
                    managed_runtime_lease=managed_runtime_lease,
                    managed_runtime=runtime,
                    managed_context=context,
                    managed_page=existing_page,
                )

        probe_page = None
        probe_context = context
        keep_probe_page = False
        try:
            now = time.time()
            if session.last_active_probe_time and now - session.last_active_probe_time < 10:
                return False
            session.last_active_probe_time = now

            if managed_runtime_lease is not None:
                probe_page, probe_context = await account_browser_runtime_manager.get_fresh_page(
                    managed_runtime_lease
                )
            else:
                probe_page = await context.new_page()
            await probe_page.goto('https://www.goofish.com/im', wait_until='domcontentloaded', timeout=30000)
            await probe_page.wait_for_timeout(1500)

            probe_url = probe_page.url
            probe_cookie_dict = await self._context_cookie_dict(probe_context)
            im_root = await probe_page.query_selector('.rc-virtual-list-holder-inner')
            has_im_root = im_root is not None

            if self._is_logged_in_url(probe_url):
                keep_probe_page = True
                logger.info(
                    f"扫码登录浏览器侧探测成功: {session.session_id}, "
                    f"probe_url: {probe_url}, has_im_root: {has_im_root}"
                )
                return self._mark_session_success(
                    session,
                    probe_cookie_dict,
                    'browser',
                    require_complete_cookies=True,
                    managed_runtime_lease=managed_runtime_lease,
                    managed_runtime=runtime,
                    managed_context=probe_context,
                    managed_page=probe_page,
                )
        except Exception as e:
            logger.debug(f"扫码登录浏览器侧探测未确认成功: {session.session_id}, 错误: {e}")
        finally:
            if probe_page and not keep_probe_page:
                try:
                    await probe_page.close()
                except Exception:
                    pass
                finally:
                    self._untrack_runtime_lease_page(managed_runtime_lease, probe_page)

        return False

    def _should_keep_session_browser_handles(
        self,
        session: Optional[QRLoginSession],
        runtime,
        context,
    ) -> bool:
        """判断当前浏览器 runtime/context 是否已经移交给后续 cookie 刷新链路"""
        return bool(
            session and
            session.status == 'success' and
            session.managed_runtime is runtime and
            session.managed_context is context
        )

    def _should_keep_session_page(
        self,
        session: Optional[QRLoginSession],
        page,
    ) -> bool:
        """判断当前标签页是否就是后续链路要继续复用的标签页"""
        return bool(
            session and
            session.status == 'success' and
            session.managed_page is page
        )

    async def _close_managed_browser_handles(self, runtime, context, page):
        """关闭扫码验证页暂存的浏览器句柄。"""
        for close_target in (page, context, runtime):
            if not close_target:
                continue
            close_method = getattr(close_target, "close", None)
            if not callable(close_method):
                continue
            try:
                await close_method()
            except Exception as close_error:
                logger.debug(f"关闭扫码登录暂存句柄失败，忽略: {close_error}")

    async def _launch_verification_page(self, session_id: str):
        """在服务端打开验证页面并截取二维码，保持原始会话存活"""
        session = self.sessions.get(session_id)
        if not session or not session.verification_url:
            return

        browser = None
        context = None
        page = None
        runtime_lease = None
        launch_error_message = None

        try:

            logger.info(f"开始打开扫码登录验证页面: {session_id}")
            runtime_lease, browser, context, show_browser = await self._launch_verification_browser_context(session)
            self._update_session_state(
                session,
                phase='verification_browser_ready',
                browser_alive=True,
                clear_error=True,
            )

            browser_cookies = self._build_cross_domain_browser_cookies(session.verification_url, session.cookies)
            if browser_cookies:
                await context.add_cookies(browser_cookies)
                logger.info(f"扫码登录验证页已注入 {len(browser_cookies)} 个跨域 Cookie: {session_id}")

            page = await self._get_or_create_context_page(context)
            self._track_runtime_lease_page(runtime_lease, page)
            try:
                logger.info(f"扫码登录验证页预热闲鱼首页: {session_id}")
                await page.goto('https://www.goofish.com/', wait_until='domcontentloaded', timeout=15000)
                await page.wait_for_timeout(1200 if show_browser else 800)
            except Exception as warmup_error:
                logger.warning(f"扫码登录验证页预热失败（继续主流程）: {session_id}, 错误: {warmup_error}")
            await page.goto(session.verification_url, wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_timeout(2500)

            screenshot_bytes = await page.screenshot(full_page=True)
            if screenshot_bytes:
                screenshot_path = image_manager.save_image(screenshot_bytes)
                if screenshot_path:
                    if session.screenshot_path and session.screenshot_path != screenshot_path:
                        image_manager.delete_image(session.screenshot_path)
                    session.screenshot_path = screenshot_path
                    self._update_session_state(
                        session,
                        phase='awaiting_verification',
                        browser_alive=True,
                    )
                    logger.info(f"扫码登录验证截图已保存: {session_id}, 路径: {screenshot_path}")
                else:
                    logger.warning(f"扫码登录验证截图保存失败: {session_id}")
            else:
                logger.warning(f"扫码登录验证截图为空: {session_id}")

            while True:
                current_session = self.sessions.get(session_id)
                if not current_session:
                    break
                if current_session.status == 'success':
                    logger.info(f"扫码登录验证页检测到会话已成功: {session_id}")
                    break
                if current_session.status not in {'verification_required', 'scanned', 'waiting', 'processing', 'confirmed'}:
                    break

                if await self._probe_browser_login_success(
                    current_session,
                    page,
                    context,
                    managed_runtime_lease=runtime_lease,
                    managed_runtime=browser,
                ):
                    break

                await page.wait_for_timeout(3000)

        except asyncio.CancelledError:
            logger.info(f"扫码登录验证页面任务已取消: {session_id}")
            raise
        except Exception as e:
            launch_error_message = str(e)
            logger.error(f"打开扫码登录验证页面失败: {session_id}, 错误: {e}")
        finally:
            if launch_error_message:
                self._update_session_state(
                    self.sessions.get(session_id),
                    status='failed',
                    phase='verification_failed',
                    error_message=launch_error_message,
                    browser_alive=False,
                )
            latest_session = self.sessions.get(session_id)
            keep_session_handles = self._should_keep_session_browser_handles(
                latest_session,
                browser,
                context,
            )
            keep_current_page = self._should_keep_session_page(latest_session, page)
            released_runtime_lease = False
            if runtime_lease is not None and not keep_session_handles:
                try:
                    await account_browser_runtime_manager.release_runtime(
                        runtime_lease,
                        reason='qr_login_verification_page_closed',
                    )
                    released_runtime_lease = True
                except Exception as release_error:
                    logger.debug(f"释放扫码登录 verification runtime lease 失败，降级为关闭句柄: {release_error}")
            try:
                if page and not keep_current_page and not released_runtime_lease:
                    await page.close()
            except Exception:
                pass
            finally:
                if page and not keep_current_page and not released_runtime_lease:
                    self._untrack_runtime_lease_page(runtime_lease, page)
            try:
                if context and not keep_session_handles and not released_runtime_lease:
                    await context.close()
            except Exception:
                pass
            try:
                if browser and not keep_session_handles and not released_runtime_lease:
                    await browser.close()
            except Exception:
                pass
            if latest_session:
                if not keep_session_handles:
                    latest_session.browser_alive = False
                latest_session.verification_task = None
                if not keep_session_handles:
                    latest_session.browser_alive = False
                if (
                    latest_session.status == 'verification_required'
                    and not keep_session_handles
                    and not latest_session.error_message
                ):
                    self._update_session_state(
                        latest_session,
                        status='failed',
                        phase='verification_failed',
                        error_message='验证页面已退出，登录未完成',
                        browser_alive=False,
                    )

            logger.info(f"扫码登录验证页面已关闭: {session_id}")

    def _ensure_verification_task(self, session: QRLoginSession):
        """确保风控验证页面任务只启动一次"""
        task = session.verification_task
        if task and not task.done():
            return
        session.verification_task = asyncio.create_task(self._launch_verification_page(session.session_id))

    def _cleanup_session_assets(self, session: QRLoginSession, *, reason: str = 'qr_login_session_cleanup'):
        """清理会话关联的截图和后台任务"""
        task = session.verification_task
        if task and not task.done():
            task.cancel()
        session.verification_task = None

        if session.screenshot_path:
            image_manager.delete_image(session.screenshot_path)
            session.screenshot_path = None

        managed_runtime_lease = session.managed_runtime_lease
        managed_runtime = session.managed_runtime
        managed_context = session.managed_context
        managed_page = session.managed_page
        session.managed_runtime_lease = None
        session.managed_runtime = None
        session.managed_context = None
        session.managed_page = None

        if not any((managed_runtime_lease, managed_runtime, managed_context, managed_page)):
            return

        async def _release_or_close_handles():
            if managed_runtime_lease is not None:
                try:
                    await account_browser_runtime_manager.release_runtime(
                        managed_runtime_lease,
                        reason=reason,
                    )
                    return
                except Exception as release_error:
                    logger.debug(f"释放扫码登录 runtime lease 失败，降级为关闭句柄: {release_error}")
            await self._close_managed_browser_handles(
                managed_runtime,
                managed_context,
                managed_page,
            )

        close_coro = _release_or_close_handles()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(close_coro)
        else:
            loop.create_task(close_coro)

    def release_session_assets(self, session_id: str, *, reason: str = 'qr_login_session_cleanup') -> None:
        session = self.sessions.get(session_id)
        if not session:
            return
        self._cleanup_session_assets(session, reason=reason)

    async def _get_mh5tk(self, session: QRLoginSession) -> dict:
        """获取m_h5_tk和m_h5_tk_enc"""
        data = {"bizScene": "home"}
        data_str = json.dumps(data, separators=(',', ':'))
        t = str(int(time.time() * 1000))
        app_key = "34839810"

        # 先发一次 GET 请求，获取 cookie 中的 m_h5_tk
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            proxy=self._resolve_session_proxy_url(session),
        ) as client:
            try:
                resp = await client.get(self.api_h5_tk, headers=self.headers)
                cookies = {k: v for k, v in resp.cookies.items()}
                session.cookies.update(cookies)

                m_h5_tk = cookies.get("m_h5_tk", "")
                token = m_h5_tk.split("_")[0] if "_" in m_h5_tk else ""

                # 生成签名
                sign_input = f"{token}&{t}&{app_key}&{data_str}"
                sign = hashlib.md5(sign_input.encode()).hexdigest()

                # 构造最终请求参数
                params = {
                    "jsv": "2.7.2",
                    "appKey": app_key,
                    "t": t,
                    "sign": sign,
                    "v": "1.0",
                    "type": "originaljson",
                    "dataType": "json",
                    "timeout": 20000,
                    "api": "mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get",
                    "data": data_str,
                }

                # 发请求正式获取数据，确保 token 有效
                await client.post(self.api_h5_tk, params=params, headers=self.headers, cookies=session.cookies)

                return cookies
            except httpx.ConnectTimeout:
                logger.error("获取m_h5_tk时连接超时")
                raise
            except httpx.ReadTimeout:
                logger.error("获取m_h5_tk时读取超时")
                raise
            except httpx.ConnectError:
                logger.error("获取m_h5_tk时连接错误")
                raise

    async def _get_login_params(self, session: QRLoginSession) -> dict:
        """获取二维码登录时需要的表单参数"""
        params = {
            "lang": "zh_cn",
            "appName": "xianyu",
            "appEntrance": "web",
            "styleType": "vertical",
            "bizParams": "",
            "notLoadSsoView": False,
            "notKeepLogin": False,
            "isMobile": False,
            "qrCodeFirst": False,
            "stie": 77,
            "rnd": random(),
        }

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.timeout,
            proxy=self._resolve_session_proxy_url(session),
        ) as client:
            try:
                resp = await client.get(
                    self.api_mini_login,
                    params=params,
                    cookies=session.cookies,
                    headers=self.headers,
                )

                # 正则匹配需要的json数据
                pattern = r"window\.viewData\s*=\s*(\{.*?\});"
                match = re.search(pattern, resp.text)
                if match:
                    json_string = match.group(1)
                    view_data = json.loads(json_string)
                    data = view_data.get("loginFormData")
                    if data:
                        data["umidTag"] = "SERVER"
                        session.params.update(data)
                        return data
                    else:
                        raise GetLoginParamsError("未找到loginFormData")
                else:
                    raise GetLoginParamsError("获取登录参数失败")
            except httpx.ConnectTimeout:
                logger.error("获取登录参数时连接超时")
                raise
            except httpx.ReadTimeout:
                logger.error("获取登录参数时读取超时")
                raise
            except httpx.ConnectError:
                logger.error("获取登录参数时连接错误")
                raise
    
    async def generate_qr_code(self, user_id: Optional[int] = None, account_id: Optional[str] = None) -> Dict[str, Any]:
        """生成二维码"""
        try:
            account_id = str(account_id or '').strip()
            if not account_id:
                return {'success': False, 'message': '缺少account_id'}

            # 创建新的会话
            session_id = str(uuid.uuid4())
            session = QRLoginSession(session_id, user_id=user_id, account_id=account_id)

            # 1. 获取m_h5_tk
            await self._get_mh5tk(session)
            logger.info(f"获取m_h5_tk成功: {session_id}")

            # 2. 获取登录参数
            login_params = await self._get_login_params(session)
            logger.info(f"获取登录参数成功: {session_id}")

            # 3. 生成二维码
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=self.timeout,
                proxy=self._resolve_session_proxy_url(session),
            ) as client:
                resp = await client.get(
                    self.api_generate_qr,
                    params=login_params,
                    headers=self.headers
                )
                logger.debug(f"[调试] 获取二维码接口原始响应: {resp.text}")

                try:
                    results = resp.json()
                    logger.debug(f"[调试] 获取二维码接口解析后: {json.dumps(results, ensure_ascii=False)}")
                except Exception as e:
                    logger.exception("二维码接口返回不是JSON")
                    raise GetLoginQRCodeError(f"二维码接口返回异常: {resp.text}")

                if results.get("content", {}).get("success") == True:
                    # 更新会话参数
                    session.params.update({
                        "t": results["content"]["data"]["t"],
                        "ck": results["content"]["data"]["ck"],
                    })

                    # 获取二维码内容
                    qr_content = results["content"]["data"]["codeContent"]
                    session.qr_content = qr_content

                    # 生成二维码图片（base64格式）
                    qr = qrcode.QRCode(
                        version=5,
                        error_correction=qrcode.constants.ERROR_CORRECT_L,
                        box_size=10,
                        border=2,
                    )
                    qr.add_data(qr_content)
                    qr.make()

                    # 将二维码转换为base64
                    from io import BytesIO
                    import base64

                    qr_img = qr.make_image()
                    buffer = BytesIO()
                    qr_img.save(buffer, format='PNG')
                    qr_base64 = base64.b64encode(buffer.getvalue()).decode()
                    qr_data_url = f"data:image/png;base64,{qr_base64}"

                    session.qr_code_url = qr_data_url
                    session.status = 'waiting'

                    # 保存会话
                    self.sessions[session_id] = session

                    # 启动状态检查任务
                    asyncio.create_task(self._monitor_qr_status(session_id))

                    logger.info(f"二维码生成成功: {session_id}")
                    return {
                        'success': True,
                        'account_id': account_id,
                        'session_id': session_id,
                        'qr_code_url': qr_data_url
                    }
                else:
                    raise GetLoginQRCodeError("获取登录二维码失败")

        except httpx.ConnectTimeout as e:
            logger.error(f"连接超时: {e}")
            return {'success': False, 'message': f'连接超时，请检查网络或尝试使用代理'}
        except httpx.ReadTimeout as e:
            logger.error(f"读取超时: {e}")
            return {'success': False, 'message': f'读取超时，服务器响应过慢'}
        except httpx.ConnectError as e:
            logger.error(f"连接错误: {e}")
            return {'success': False, 'message': f'连接错误，请检查网络或代理设置'}
        except Exception as e:
            logger.exception("二维码生成过程中发生异常")
            return {'success': False, 'message': f'生成二维码失败: {str(e)}'}
    
    async def _poll_qrcode_status(self, session: QRLoginSession) -> httpx.Response:
        """获取二维码扫描状态"""
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.timeout,
            proxy=self._resolve_session_proxy_url(session),
        ) as client:
            resp = await client.post(
                self.api_scan_status,
                data=session.params,
                cookies=session.cookies,
                headers=self.headers,
            )
            return resp

    async def _monitor_qr_status(self, session_id: str):
        """监控二维码状态"""
        try:
            session = self.sessions.get(session_id)
            if not session:
                return

            logger.info(f"开始监控二维码状态: {session_id}")

            # 监控登录状态
            max_wait_time = 300  # 5分钟
            start_time = time.time()

            while time.time() - start_time < max_wait_time:
                try:
                    # 检查会话是否还存在
                    if session_id not in self.sessions:
                        break
                    if session.status == 'success':
                        logger.info(f"扫码登录API轮询检测到会话已成功: {session_id}")
                        break

                    # 轮询二维码状态
                    resp = await self._poll_qrcode_status(session)
                    if session.status == 'success':
                        logger.info(f"扫码登录API轮询响应返回前，会话已由其他链路成功: {session_id}")
                        break
                    qrcode_status = (
                        resp.json()
                        .get("content", {})
                        .get("data", {})
                        .get("qrCodeStatus")
                    )

                    if qrcode_status == "CONFIRMED":
                        if self._handle_confirmed_qr_response(session, resp):
                            break
                        await asyncio.sleep(0.8)
                        continue

                    elif qrcode_status == "NEW":
                        self._update_session_state(session, phase='waiting_for_scan')
                        # 二维码未被扫描，继续轮询
                        continue

                    elif qrcode_status == "EXPIRED":
                        # 二维码已过期
                        if session.status == 'verification_required':
                            logger.info(f"二维码已过期，但会话已进入验证流程，继续等待: {session_id}")
                        else:
                            self._update_session_state(
                                session,
                                status='expired',
                                phase='expired',
                                error_message='二维码已过期',
                                browser_alive=False,
                            )
                            logger.info(f"二维码已过期: {session_id}")
                            break

                    elif qrcode_status == "SCANED":
                        # 二维码已被扫描，等待确认
                        if session.status in {'waiting', 'confirmed'}:
                            self._update_session_state(
                                session,
                                status='scanned',
                                phase='awaiting_confirmation',
                                clear_error=True,
                            )
                            logger.info(f"二维码已扫描，等待确认: {session_id}")
                    else:
                        # 用户取消确认
                        if session.status == 'verification_required':
                            logger.info(f"扫码状态 {qrcode_status}，但验证流程仍在进行，继续等待: {session_id}")
                        else:
                            self._update_session_state(
                                session,
                                status='cancelled',
                                phase='cancelled',
                                error_message='用户取消登录',
                                browser_alive=False,
                            )
                            logger.info(f"用户取消登录: {session_id}")
                            break

                    await asyncio.sleep(0.8)  # 每0.8秒检查一次

                except Exception as e:
                    logger.error(f"监控二维码状态异常: {e}")
                    await asyncio.sleep(2)

            # 超时处理
            if session.status not in ['success', 'expired', 'cancelled', 'verification_required', 'failed', 'confirmed']:
                self._update_session_state(
                    session,
                    status='expired',
                    phase='expired',
                    error_message='二维码监控超时',
                    browser_alive=False,
                )
                logger.info(f"二维码监控超时，标记为过期: {session_id}")

        except Exception as e:
            logger.error(f"监控二维码状态失败: {e}")
            if session_id in self.sessions:
                self._update_session_state(
                    self.sessions[session_id],
                    status='failed',
                    phase='monitor_failed',
                    error_message=str(e),
                    browser_alive=False,
                )
    
    def get_session_status(self, session_id: str) -> Dict[str, Any]:
        """获取会话状态"""
        session = self.sessions.get(session_id)
        if not session:
            return {'status': 'not_found'}

        if session.is_expired() and session.status != 'success':
            self._update_session_state(
                session,
                status='expired',
                phase='expired',
                error_message=session.error_message or '二维码已过期',
                browser_alive=False,
            )

        verification_type = session.verification_type or self._detect_verification_type(session.verification_url)
        verification_type_label = {
            'face_verify': '人脸验证',
            'sms_verify': '短信验证',
            'qr_verify': '二维码验证',
            'identity_verify': '身份验证',
            None: None,
        }.get(verification_type, verification_type)

        result = {
            'status': session.status,
            'session_id': session_id,
            'user_id': session.user_id,
            'account_id': session.account_id,
            'phase': session.phase,
            'verification_type': verification_type,
            'verification_type_label': verification_type_label,
            'handoff_status': session.handoff_status,
            'handoff_error': session.handoff_error,
            'success_stage': session.success_stage,
            'browser_alive': session.browser_alive,
        }
        if session.error_message:
            result['error'] = session.error_message
        logger.info(f"获取会话状态: {result}")
        # 如果需要验证，返回验证URL
        if session.status == 'verification_required':
            result['verification_url'] = session.verification_url
            result['screenshot_path'] = session.screenshot_path
            result['message'] = (
                f'需要{verification_type_label}，请查看验证截图'
                if session.screenshot_path else
                f'需要{verification_type_label}，请继续完成验证'
            )

        # 如果登录成功，返回Cookie信息
        elif session.status == 'confirmed':
            result['message'] = '扫码已确认，正在等待完整登录Cookie'
        elif session.status == 'failed' and session.error_message:
            result['message'] = session.error_message

        if session.status == 'success' and session.cookies and session.unb:
            result['cookies'] = self._cookie_marshal(session.cookies)
            result['unb'] = session.unb
        if session.account_info:
            result['account_info'] = session.account_info

        return result

    def cleanup_expired_sessions(self):
        """清理过期会话"""
        expired_sessions = []
        for session_id, session in self.sessions.items():
            if session.is_expired():
                expired_sessions.append(session_id)

        for session_id in expired_sessions:
            self._cleanup_session_assets(self.sessions[session_id])
            del self.sessions[session_id]
            logger.info(f"清理过期会话: {session_id}")

    def get_session_cookies(self, session_id: str) -> Optional[Dict[str, Any]]:
        """获取会话Cookie"""
        session = self.sessions.get(session_id)
        if session and session.status == 'success':
            return {
                'account_id': session.account_id,
                'cookies': self._cookie_marshal(session.cookies),
                'unb': session.unb,
                'managed_runtime_lease': session.managed_runtime_lease,
                'managed_runtime': session.managed_runtime,
                'managed_context': session.managed_context,
                'managed_page': session.managed_page,
            }
        return None

# 全局二维码登录管理器实例
qr_login_manager = QRLoginManager()
