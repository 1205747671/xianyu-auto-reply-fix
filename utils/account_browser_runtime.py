from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import inspect
import json
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from loguru import logger
from utils import browser_provider as _browser_provider

launch_managed_browser_runtime = getattr(
    _browser_provider,
    "launch_managed_browser_runtime",
    lambda **kwargs: None,
)
launch_managed_browser_runtime_async = getattr(
    _browser_provider,
    "launch_managed_browser_runtime_async",
    lambda **kwargs: None,
)
close_managed_runtime_handle = getattr(
    _browser_provider,
    "close_managed_runtime_handle",
    None,
)
close_managed_runtime_handle_async = getattr(
    _browser_provider,
    "close_managed_runtime_handle_async",
    None,
)

RuntimeFactory = Callable[[str, str, int, str, bool], Awaitable[Any]]
RuntimeCloser = Callable[[Any], Awaitable[Any]]
SyncRuntimeFactory = Callable[[str, str, int, str, bool], Any]
SyncRuntimeCloser = Callable[[Any], Any]

ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _stable_cloakbrowser_fingerprint_seed(account_id: str) -> str:
    """为 CloakBrowser 的 --fingerprint 生成稳定 seed。

    CloakBrowser 默认每次 launch 会随机生成 fingerprint seed；若不固定 seed，复用 user_data_dir 也可能
    因“设备画像”变化导致会话不稳定（频繁要求重新登录）。
    """
    override = os.environ.get("XY_CLOAKBROWSER_FINGERPRINT", "").strip()
    if override:
        return override
    digest = hashlib.sha256(f"{account_id}:cloak_fingerprint".encode("utf-8")).hexdigest()
    value = int(digest[:12], 16)
    return str(10000 + (value % 90000))


def _ensure_cloakbrowser_fingerprint_arg(account_id: str, launch_options: Dict[str, Any]) -> None:
    args = list(launch_options.get("args") or [])
    if any(str(arg).startswith("--fingerprint=") for arg in args):
        return
    args.append(f"--fingerprint={_stable_cloakbrowser_fingerprint_seed(account_id)}")
    launch_options["args"] = args


@dataclass
class AccountBrowserRuntimeLease:
    account_id: str
    purpose: str
    exclusive: bool
    generation: int
    profile_dir: str
    runtime: Any
    pages: list[Any] = field(default_factory=list)
    released: bool = False


@dataclass
class SyncAccountBrowserRuntimeLease:
    account_id: str
    purpose: str
    exclusive: bool
    generation: int
    profile_dir: str
    runtime: Any
    pages: list[Any] = field(default_factory=list)
    released: bool = False


@dataclass
class _RuntimeState:
    generation: int = 0
    runtime: Any = None
    runtime_identity: Optional[Tuple[str, bool, str]] = None
    claimed_profile_dir: Optional[str] = None
    claim_owner: Optional[Tuple[int, str, str]] = None
    current_purpose: Optional[str] = None
    active_leases: int = 0
    active_exclusive: bool = False
    last_released_at: float = 0.0
    pending_closures: list[tuple[Any, str]] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)


@dataclass
class _SyncRuntimeState:
    generation: int = 0
    runtime: Any = None
    runtime_identity: Optional[Tuple[str, bool, str]] = None
    claimed_profile_dir: Optional[str] = None
    claim_owner: Optional[Tuple[int, str, str]] = None
    current_purpose: Optional[str] = None
    owner_thread_id: Optional[int] = None
    active_leases: int = 0
    active_exclusive: bool = False
    last_released_at: float = 0.0
    pending_closures: list[tuple[Any, str, Optional[int]]] = field(default_factory=list)
    condition: threading.Condition = field(default_factory=threading.Condition)


@dataclass
class _SyncAccountTaskCall:
    func: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: Dict[str, Any]
    started: threading.Event = field(default_factory=threading.Event)
    abandoned: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    exception: Optional[BaseException] = None


@dataclass
class _SyncAccountWorkerState:
    thread: Optional[threading.Thread] = None
    thread_id: Optional[int] = None
    last_used_at: float = 0.0
    task_queue: Any = field(default_factory=queue.Queue)
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop_requested: bool = False


@dataclass
class _ProfileClaimState:
    owner: Optional[Tuple[int, str, str]] = None
    owner_metadata: Optional[Dict[str, Any]] = None


@dataclass
class _OwnerModeState:
    mode: Optional[str] = None
    active_count: int = 0
    condition: threading.Condition = field(default_factory=threading.Condition)


_PROFILE_CLAIMS_GUARD = threading.Lock()
_PROFILE_CLAIMS: Dict[str, _ProfileClaimState] = {}
_MANAGER_INSTANCE_ID_GUARD = threading.Lock()
_NEXT_MANAGER_INSTANCE_ID = 1


def _safe_bool_call(target: Any, method_name: str) -> Optional[bool]:
    method = getattr(target, method_name, None)
    if not callable(method):
        return None
    try:
        return bool(method())
    except Exception:
        return False


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _runtime_is_alive(runtime: Any) -> bool:
    if runtime is None:
        return False
    is_managed_cdp_runtime = bool(getattr(runtime, "cdp_endpoint", None))
    is_alive = getattr(runtime, "is_alive", None)
    runtime_is_alive = None
    if callable(is_alive):
        try:
            runtime_is_alive = bool(is_alive())
        except Exception:
            runtime_is_alive = False
    browser = getattr(runtime, "browser", None)
    browser_connected = None
    if browser is not None:
        browser_connected = _safe_bool_call(browser, "is_connected")
        if browser_connected is False:
            return False
    context = getattr(runtime, "context", None)
    context_closed = None
    if context is not None:
        context_closed = _safe_bool_call(context, "is_closed")
        if context_closed is True:
            return False
    page = getattr(runtime, "page", None)
    if page is not None:
        closed = _safe_bool_call(page, "is_closed")
        if closed is True:
            runtime.page = None
    if is_managed_cdp_runtime:
        # CloakBrowser's launcher process may exit after handing off the real
        # Chromium instance. For managed CDP runtimes, prefer live Playwright
        # connectivity signals over launcher process liveness.
        if browser_connected is True:
            return True
        if context is not None and context_closed is False and browser_connected is not False:
            return True
        if runtime_is_alive is True:
            return True
    elif runtime_is_alive is not None:
        return runtime_is_alive
    process = getattr(runtime, "process", None)
    if process is None:
        return True
    poll = getattr(process, "poll", None)
    if callable(poll):
        try:
            return poll() is None
        except Exception:
            return False
    return True


def resolve_runtime_attach_metadata(
    runtime: Any,
    runtime_request: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Optional[str]]:
    request = dict(runtime_request or {})
    runtime_browser_features = getattr(runtime, "browser_features", None) or {}
    browser_features = dict(runtime_browser_features or request.get("browser_features") or {})
    profile_id = getattr(runtime, "profile_id", None) or request.get("profile_id")
    return browser_features, profile_id


def capture_owner_lock_token(owner_lock: Any) -> Any:
    capture_owner_token = getattr(owner_lock, "capture_owner_token", None)
    if callable(capture_owner_token):
        return capture_owner_token()
    return None


def release_owner_lock_if_owned(owner_lock: Any, owner_token: Any = None) -> bool:
    if owner_lock is None:
        return False
    release_if_owner = getattr(owner_lock, "release_by_token_if_owner", None)
    if callable(release_if_owner):
        if owner_token is None:
            return False
        return bool(release_if_owner(owner_token))

    release_by_token = getattr(owner_lock, "release_by_token", None)
    if callable(release_by_token):
        if owner_token is None:
            return False
        try:
            release_by_token(owner_token)
            return True
        except RuntimeError:
            return False

    locked = getattr(owner_lock, "locked", None)
    if callable(locked) and locked():
        owner_lock.release()
        return True
    return False


def _call_runtime_factory(factory: Callable[..., Any], *args, runtime_request: Optional[Dict[str, Any]] = None) -> Any:
    parameters = inspect.signature(factory).parameters
    accepts_var_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if runtime_request is not None and ("runtime_request" in parameters or accepts_var_kwargs):
        return factory(*args, runtime_request=runtime_request)
    return factory(*args)


def _thread_id_is_alive(thread_id: Optional[int]) -> bool:
    if thread_id is None:
        return False
    return any(thread.ident == thread_id and thread.is_alive() for thread in threading.enumerate())


def _allocate_manager_instance_id() -> int:
    global _NEXT_MANAGER_INSTANCE_ID
    with _MANAGER_INSTANCE_ID_GUARD:
        manager_instance_id = _NEXT_MANAGER_INSTANCE_ID
        _NEXT_MANAGER_INSTANCE_ID += 1
    return manager_instance_id


def _normalize_profile_dir_key(profile_dir: str) -> str:
    return str(Path(profile_dir).resolve())


def _normalize_runtime_purpose(purpose: Any) -> str:
    normalized = str(purpose or "").strip()
    return normalized or "unknown"


def _build_profile_claim_metadata(
    *,
    purpose: Any,
    thread_id: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "purpose": _normalize_runtime_purpose(purpose),
        "thread_id": thread_id,
        "updated_at": time.time(),
    }


def _format_profile_claim_owner(
    owner: Optional[Tuple[int, str, str]],
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    if owner is None:
        return "unknown-owner"
    manager_instance_id, account_id, mode = owner
    details = [
        f"manager={manager_instance_id}",
        f"account_id={account_id}",
        f"mode={mode}",
    ]
    safe_metadata = dict(metadata or {})
    purpose = _normalize_runtime_purpose(safe_metadata.get("purpose"))
    if purpose and purpose != "unknown":
        details.append(f"purpose={purpose}")
    thread_id = safe_metadata.get("thread_id")
    if thread_id is not None:
        details.append(f"thread_id={thread_id}")
    return ", ".join(details)


def _claim_profile_dir(
    profile_dir: str,
    owner: Tuple[int, str, str],
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    profile_dir_key = _normalize_profile_dir_key(profile_dir)
    with _PROFILE_CLAIMS_GUARD:
        claim_state = _PROFILE_CLAIMS.setdefault(profile_dir_key, _ProfileClaimState())
        if claim_state.owner is None:
            claim_state.owner = owner
            claim_state.owner_metadata = dict(metadata or {})
            return profile_dir_key
        if claim_state.owner == owner:
            claim_state.owner_metadata = dict(metadata or claim_state.owner_metadata or {})
            return profile_dir_key
        formatted_existing_owner = _format_profile_claim_owner(
            claim_state.owner,
            claim_state.owner_metadata,
        )
        formatted_requested_owner = _format_profile_claim_owner(owner, metadata)
        logger.warning(
            "账号级 browser profile claim 冲突: "
            f"profile_dir={profile_dir_key}, current_owner={formatted_existing_owner}, "
            f"requested_owner={formatted_requested_owner}"
        )
        raise RuntimeError(
            "账号级 browser profile 已被其他 runtime 持有，拒绝并发复用: "
            f"profile_dir={profile_dir_key}, owner={formatted_existing_owner}, "
            f"requested_owner={formatted_requested_owner}"
        )


def _release_profile_dir(profile_dir: Optional[str], owner: Optional[Tuple[int, str, str]]) -> None:
    if not profile_dir or owner is None:
        return
    profile_dir_key = _normalize_profile_dir_key(profile_dir)
    with _PROFILE_CLAIMS_GUARD:
        claim_state = _PROFILE_CLAIMS.get(profile_dir_key)
        if claim_state is None or claim_state.owner != owner:
            return
        _PROFILE_CLAIMS.pop(profile_dir_key, None)


async def _close_async_page(page: Any) -> None:
    if page is None:
        return
    close = getattr(page, "close", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def _close_sync_page(page: Any) -> None:
    if page is None:
        return
    close = getattr(page, "close", None)
    if not callable(close):
        return
    close()


def _reuse_runtime_page_if_available(runtime: Any, context: Any, lease_pages: list[Any]) -> Optional[Any]:
    if lease_pages:
        return None
    page = getattr(runtime, "page", None)
    if page is None:
        return None
    if _safe_bool_call(page, "is_closed") is True:
        runtime.page = None
        return None

    context_pages = getattr(context, "pages", None)
    if isinstance(context_pages, (list, tuple)) and context_pages and page not in context_pages:
        return None
    return page


def _should_preserve_managed_runtime_page(runtime: Any, page: Any) -> bool:
    if runtime is None or page is None:
        return False
    if page is not getattr(runtime, "page", None):
        return False
    if not getattr(runtime, "cdp_endpoint", None):
        return False
    if _safe_bool_call(page, "is_closed") is True:
        return False
    context = getattr(runtime, "context", None)
    context_pages = getattr(context, "pages", None)
    if isinstance(context_pages, (list, tuple)) and len(context_pages) > 1:
        return False
    return True


def _pop_locale_and_timezone(options: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    locale = options.pop("locale", None)
    timezone = options.pop("timezone", None)
    if timezone is None and "timezone_id" in options:
        timezone = options.pop("timezone_id", None)
    return locale, timezone


def _resolve_managed_launch_options(
    *,
    request: Dict[str, Any],
    launch_options: Dict[str, Any],
    context_options: Dict[str, Any],
    persistent_context_options: Dict[str, Any],
    use_persistent_context: bool,
) -> Dict[str, Any]:
    managed_launch_options = dict(launch_options)
    if "headless" in request and "headless" not in managed_launch_options:
        managed_launch_options["headless"] = bool(request.get("headless"))
    managed_launch_options.setdefault("headless", True)

    if use_persistent_context:
        locale, timezone = _pop_locale_and_timezone(persistent_context_options)
        fallback_locale, fallback_timezone = _pop_locale_and_timezone(context_options)
        locale = locale if locale is not None else fallback_locale
        timezone = timezone if timezone is not None else fallback_timezone
    else:
        locale, timezone = _pop_locale_and_timezone(context_options)
        fallback_locale, fallback_timezone = _pop_locale_and_timezone(persistent_context_options)
        locale = locale if locale is not None else fallback_locale
        timezone = timezone if timezone is not None else fallback_timezone

    if "timezone_id" in managed_launch_options and "timezone" not in managed_launch_options:
        managed_launch_options["timezone"] = managed_launch_options.pop("timezone_id")
    else:
        managed_launch_options.pop("timezone_id", None)
    if locale is not None and "locale" not in managed_launch_options:
        managed_launch_options["locale"] = locale
    if timezone is not None and "timezone" not in managed_launch_options:
        managed_launch_options["timezone"] = timezone
    return managed_launch_options


def _get_browser_contexts(browser: Any) -> list[Any]:
    contexts = getattr(browser, "contexts", None)
    if callable(contexts):
        contexts = contexts()
    return list(contexts or [])


async def _default_async_runtime_factory(
    account_id: str,
    profile_dir: str,
    generation: int,
    purpose: str,
    exclusive: bool,
    *,
    runtime_request: Optional[Dict[str, Any]] = None,
) -> Any:
    _ = account_id, generation, purpose, exclusive
    request = dict(runtime_request or {})
    browser_features = dict(request.get("browser_features") or {})
    use_persistent_context = bool(request.get("use_persistent_context", True))
    launch_options = dict(request.get("launch_options") or {})
    context_options = dict(request.get("context_options") or {})
    persistent_context_options = dict(request.get("persistent_context_options") or {})
    initial_cookie_payload = list(request.get("initial_cookie_payload") or [])
    profile_dir = str(request.get("profile_dir") or profile_dir)
    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    managed_launch_options = _resolve_managed_launch_options(
        request=request,
        launch_options=launch_options,
        context_options=context_options,
        persistent_context_options=persistent_context_options,
        use_persistent_context=use_persistent_context,
    )
    _ensure_cloakbrowser_fingerprint_arg(account_id, managed_launch_options)
    runtime = await _maybe_await(
        launch_managed_browser_runtime_async(
            user_data_dir=profile_dir,
            **managed_launch_options,
        )
    )
    browser = getattr(runtime, "browser", None)
    if browser is None:
        raise RuntimeError("managed runtime browser is unavailable")
    if use_persistent_context:
        contexts = _get_browser_contexts(browser)
        if not contexts:
            raise RuntimeError("managed runtime browser has no attached persistent context")
        context = contexts[0]
    else:
        new_context = getattr(browser, "new_context", None)
        if not callable(new_context):
            raise RuntimeError("managed runtime browser does not support new_context")
        context = await _maybe_await(new_context(**context_options))

    pages = list(getattr(context, "pages", []) or [])
    if pages:
        page = pages[0]
    else:
        new_page = getattr(context, "new_page", None)
        if not callable(new_page):
            raise RuntimeError("async runtime context does not support new_page")
        page = await _maybe_await(new_page())
    if initial_cookie_payload:
        add_cookies = getattr(context, "add_cookies", None)
        if not callable(add_cookies):
            raise RuntimeError("async runtime context does not support add_cookies")
        await _maybe_await(add_cookies(initial_cookie_payload))
    runtime.browser = browser
    runtime.context = context
    runtime.page = page
    runtime.profile_dir = profile_dir
    runtime.browser_features = browser_features
    runtime.profile_id = request.get("profile_id")
    return runtime


async def _default_async_runtime_closer(runtime: Any, *, reason: str) -> Any:
    _ = reason
    if getattr(runtime, "cdp_endpoint", None) and callable(close_managed_runtime_handle_async):
        return await _maybe_await(close_managed_runtime_handle_async(runtime, reason=reason))
    page = getattr(runtime, "page", None)
    context = getattr(runtime, "context", None)
    browser = getattr(runtime, "browser", None)
    playwright = getattr(runtime, "playwright", None)

    async def _close_component(target: Any, method_name: str) -> None:
        if target is None:
            return
        close_method = getattr(target, method_name, None)
        if not callable(close_method):
            return
        try:
            await _maybe_await(close_method())
        except asyncio.CancelledError:
            logger.debug(
                f"default async runtime closer ignored CancelledError from {method_name}()"
            )
        except Exception:
            pass

    await _close_component(page, "close")
    await _close_component(context, "close")
    await _close_component(browser, "close")
    await _close_component(playwright, "stop")
    return runtime

def _default_sync_runtime_factory(
    account_id: str,
    profile_dir: str,
    generation: int,
    purpose: str,
    exclusive: bool,
    *,
    runtime_request: Optional[Dict[str, Any]] = None,
) -> Any:
    _ = account_id, generation, purpose, exclusive
    request = dict(runtime_request or {})
    browser_features = dict(request.get("browser_features") or {})
    use_persistent_context = bool(request.get("use_persistent_context", True))
    launch_options = dict(request.get("launch_options") or {})
    context_options = dict(request.get("context_options") or {})
    persistent_context_options = dict(request.get("persistent_context_options") or {})
    initial_cookie_payload = list(request.get("initial_cookie_payload") or [])
    profile_dir = str(request.get("profile_dir") or profile_dir)
    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    managed_launch_options = _resolve_managed_launch_options(
        request=request,
        launch_options=launch_options,
        context_options=context_options,
        persistent_context_options=persistent_context_options,
        use_persistent_context=use_persistent_context,
    )
    _ensure_cloakbrowser_fingerprint_arg(account_id, managed_launch_options)
    runtime = launch_managed_browser_runtime(
        user_data_dir=profile_dir,
        **managed_launch_options,
    )
    browser = getattr(runtime, "browser", None)
    if browser is None:
        raise RuntimeError("managed runtime browser is unavailable")
    if use_persistent_context:
        contexts = _get_browser_contexts(browser)
        if not contexts:
            raise RuntimeError("managed runtime browser has no attached persistent context")
        context = contexts[0]
    else:
        new_context = getattr(browser, "new_context", None)
        if not callable(new_context):
            raise RuntimeError("managed runtime browser does not support new_context")
        context = new_context(**context_options)

    pages = list(getattr(context, "pages", []) or [])
    page = pages[0] if pages else context.new_page()
    if initial_cookie_payload:
        context.add_cookies(initial_cookie_payload)
    runtime.browser = browser
    runtime.context = context
    runtime.page = page
    runtime.profile_dir = profile_dir
    runtime.browser_features = browser_features
    runtime.profile_id = request.get("profile_id")
    return runtime


def _default_sync_runtime_closer(runtime: Any, *, reason: str) -> Any:
    _ = reason
    if getattr(runtime, "cdp_endpoint", None) and callable(close_managed_runtime_handle):
        return close_managed_runtime_handle(runtime, reason=reason)
    page = getattr(runtime, "page", None)
    context = getattr(runtime, "context", None)
    browser = getattr(runtime, "browser", None)
    playwright = getattr(runtime, "playwright", None)

    try:
        if page is not None:
            page.close()
    except Exception:
        pass
    try:
        if context is not None:
            context.close()
    except Exception:
        pass
    try:
        if browser is not None:
            browser.close()
    except Exception:
        pass
    try:
        if playwright is not None:
            playwright.stop()
    except Exception:
        pass
    return runtime


def _resolve_runtime_profile_dir(
    resolved_profile_dir: str,
    runtime_request: Optional[Dict[str, Any]] = None,
) -> str:
    request = dict(runtime_request or {})
    requested_profile_dir = str(request.get("profile_dir") or "").strip()
    canonical_profile_dir = str(Path(resolved_profile_dir).resolve())
    if not requested_profile_dir:
        return canonical_profile_dir
    requested_profile_dir = str(Path(requested_profile_dir).resolve())
    if requested_profile_dir != canonical_profile_dir:
        raise ValueError("runtime_request.profile_dir 必须与 account_id 对应的标准 profile_dir 一致")
    return canonical_profile_dir


def _build_runtime_identity(
    profile_dir: str,
    runtime_request: Optional[Dict[str, Any]] = None,
    *,
    default_persistent_context: bool,
) -> Tuple[str, bool, str]:
    request = dict(runtime_request or {})
    use_persistent_context = request.get("use_persistent_context")
    if use_persistent_context is None:
        use_persistent_context = default_persistent_context
    else:
        use_persistent_context = bool(use_persistent_context)
    launch_options = dict(request.get("launch_options") or {})
    context_options = dict(request.get("context_options") or {})
    persistent_context_options = dict(request.get("persistent_context_options") or {})
    managed_launch_options = _resolve_managed_launch_options(
        request=request,
        launch_options=launch_options,
        context_options=context_options,
        persistent_context_options=persistent_context_options,
        use_persistent_context=use_persistent_context,
    )
    identity_payload = {
        "use_persistent_context": use_persistent_context,
        "launch_options": managed_launch_options,
        "context_options": context_options if not use_persistent_context else {},
        "persistent_context_options": persistent_context_options if use_persistent_context else {},
        "browser_features": request.get("browser_features") or {},
        "profile_id": request.get("profile_id"),
    }
    try:
        identity_signature = json.dumps(
            identity_payload,
            sort_keys=True,
            ensure_ascii=True,
            default=str,
        )
    except TypeError:
        identity_signature = repr(identity_payload)
    return (profile_dir, use_persistent_context, identity_signature)


class AccountBrowserRuntimeManager:
    def __init__(
        self,
        *,
        base_dir: Optional[str] = None,
        runtime_factory: Optional[RuntimeFactory] = None,
        runtime_closer: Optional[RuntimeCloser] = None,
        sync_runtime_factory: Optional[SyncRuntimeFactory] = None,
        sync_runtime_closer: Optional[SyncRuntimeCloser] = None,
        time_fn: Optional[Callable[[], float]] = None,
        idle_timeout_seconds: float = 300.0,
    ) -> None:
        self.base_dir = str(Path(base_dir or ".").resolve())
        self._manager_instance_id = _allocate_manager_instance_id()
        self.runtime_factory = runtime_factory or _default_async_runtime_factory
        self.runtime_closer = runtime_closer or _default_async_runtime_closer
        self.sync_runtime_factory = sync_runtime_factory or _default_sync_runtime_factory
        self.sync_runtime_closer = sync_runtime_closer or _default_sync_runtime_closer
        self.time_fn = time_fn or time.time
        self.idle_timeout_seconds = float(idle_timeout_seconds)
        self._states: Dict[str, _RuntimeState] = {}
        self._sync_states: Dict[str, _SyncRuntimeState] = {}
        self._sync_states_guard = threading.Lock()
        self._sync_account_workers: Dict[str, _SyncAccountWorkerState] = {}
        self._sync_account_workers_guard = threading.Lock()
        self._owner_mode_states: Dict[str, _OwnerModeState] = {}
        self._owner_mode_states_guard = threading.Lock()

    def _normalize_account_id(self, account_id: str) -> str:
        normalized = str(account_id or "").strip()
        if not normalized or not ACCOUNT_ID_PATTERN.fullmatch(normalized):
            raise ValueError(
                "account_id 只能包含英文字母、数字、下划线和短横线"
            )
        return normalized

    def _build_profile_claim_owner(self, account_id: str, *, mode: str) -> Tuple[int, str, str]:
        return (self._manager_instance_id, account_id, mode)

    @staticmethod
    def _raise_if_runtime_draining_pending_closures(state: Any, *, account_id: str, mode: str) -> None:
        if getattr(state, "pending_closures", None):
            raise RuntimeError(
                f"账号 {account_id} 的 {mode} runtime 正在失效回收，旧 lease 未释放，拒绝提前重建"
            )

    def _ensure_profile_claim(
        self,
        state: Any,
        *,
        account_id: str,
        profile_dir: str,
        mode: str,
        purpose: str,
        thread_id: Optional[int] = None,
    ) -> str:
        owner = self._build_profile_claim_owner(account_id, mode=mode)
        claim_metadata = _build_profile_claim_metadata(
            purpose=purpose,
            thread_id=thread_id,
        )
        if state.claimed_profile_dir == profile_dir and state.claim_owner == owner:
            _claim_profile_dir(profile_dir, owner, claim_metadata)
            return profile_dir
        claimed_profile_dir = _claim_profile_dir(profile_dir, owner, claim_metadata)
        state.claimed_profile_dir = claimed_profile_dir
        state.claim_owner = owner
        return claimed_profile_dir

    @staticmethod
    def _release_profile_claim(state: Any) -> None:
        _release_profile_dir(getattr(state, "claimed_profile_dir", None), getattr(state, "claim_owner", None))
        state.claimed_profile_dir = None
        state.claim_owner = None

    def _account_worker_loop(self, account_id: str, state: _SyncAccountWorkerState) -> None:
        current_thread_id = threading.get_ident()
        with state.lock:
            state.thread_id = current_thread_id
            state.last_used_at = self.time_fn()
            state.stop_requested = False
        try:
            while True:
                try:
                    task = state.task_queue.get(timeout=0.1)
                except queue.Empty:
                    with state.lock:
                        if state.stop_requested and state.task_queue.empty():
                            return
                    continue
                if not isinstance(task, _SyncAccountTaskCall):
                    continue
                self._execute_sync_account_task_call(state, task)
        finally:
            with state.lock:
                state.thread_id = None
                state.thread = None
                state.last_used_at = self.time_fn()
                state.stop_requested = False

    def _execute_sync_account_task_call(
        self,
        state: _SyncAccountWorkerState,
        task: _SyncAccountTaskCall,
    ) -> None:
        if task.done.is_set() or task.abandoned.is_set():
            return
        try:
            with state.lock:
                state.last_used_at = self.time_fn()
                state.stop_requested = False
            task.started.set()
            if task.done.is_set() or task.abandoned.is_set():
                return
            task.result = task.func(*task.args, **task.kwargs)
        except BaseException as exc:
            task.exception = exc
        finally:
            with state.lock:
                state.last_used_at = self.time_fn()
            if not task.done.is_set():
                task.done.set()

    def _get_sync_account_worker_state(self, account_id: str) -> _SyncAccountWorkerState:
        with self._sync_account_workers_guard:
            return self._sync_account_workers.setdefault(account_id, _SyncAccountWorkerState())

    def _ensure_sync_account_worker_thread(
        self,
        account_id: str,
        state: _SyncAccountWorkerState,
    ) -> None:
        with state.lock:
            if state.thread is not None and state.thread.is_alive():
                state.stop_requested = False
                return
            state.stop_requested = False
            state.thread = threading.Thread(
                target=self._account_worker_loop,
                args=(account_id, state),
                name=f"account-browser-{account_id}",
                daemon=True,
            )
            state.thread.start()

    def is_sync_account_worker_thread(
        self,
        account_id: str,
        *,
        thread_id: Optional[int] = None,
    ) -> bool:
        account_id = self._normalize_account_id(account_id)
        state = self._get_sync_account_worker_state(account_id)
        current_thread_id = thread_id or threading.get_ident()
        with state.lock:
            return bool(
                state.thread is not None
                and state.thread.is_alive()
                and state.thread_id == current_thread_id
            )

    def wait_for_threadsafe_future_result_on_account_thread(
        self,
        account_id: str,
        thread_future: concurrent.futures.Future,
        *,
        timeout: float,
        poll_interval: float = 0.05,
    ) -> Any:
        account_id = self._normalize_account_id(account_id)
        if not self.is_sync_account_worker_thread(account_id):
            return thread_future.result(timeout=timeout)

        state = self._get_sync_account_worker_state(account_id)
        deadline = time.monotonic() + timeout
        while True:
            try:
                return thread_future.result(timeout=0)
            except concurrent.futures.TimeoutError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                try:
                    task = state.task_queue.get(timeout=min(max(0.01, poll_interval), remaining))
                except queue.Empty:
                    continue
                if not isinstance(task, _SyncAccountTaskCall):
                    continue
                self._execute_sync_account_task_call(state, task)

    def run_sync_task_on_account_thread(
        self,
        account_id: str,
        func: Callable[..., Any],
        *args,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> Any:
        account_id = self._normalize_account_id(account_id)
        state = self._get_sync_account_worker_state(account_id)
        current_thread_id = threading.get_ident()
        with state.lock:
            if state.thread_id == current_thread_id:
                state.last_used_at = self.time_fn()
                state.stop_requested = False
                return func(*args, **kwargs)
        self._ensure_sync_account_worker_thread(account_id, state)
        call = _SyncAccountTaskCall(
            func=func,
            args=tuple(args),
            kwargs=dict(kwargs),
        )
        with state.lock:
            state.stop_requested = False
        state.task_queue.put(call)
        deadline = None if timeout is None else (time.monotonic() + max(0.0, float(timeout)))
        while True:
            if call.done.is_set():
                break
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    call.abandoned.set()
                    call.done.set()
                    raise TimeoutError(f"account browser worker task timed out: account_id={account_id}")
                wait_slice = min(0.05, remaining)
            else:
                wait_slice = 0.05
            if call.done.wait(timeout=wait_slice):
                break
            with state.lock:
                worker_thread = state.thread
                worker_alive = bool(worker_thread is not None and worker_thread.is_alive())
            if not worker_alive:
                self._ensure_sync_account_worker_thread(account_id, state)
        if call.exception is not None:
            raise call.exception
        return call.result

    async def run_sync_task_on_account_thread_async(
        self,
        account_id: str,
        func: Callable[..., Any],
        *args,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> Any:
        return await asyncio.to_thread(
            self.run_sync_task_on_account_thread,
            account_id,
            func,
            *args,
            timeout=timeout,
            **kwargs,
        )

    def _get_owner_mode_state(self, account_id: str) -> _OwnerModeState:
        with self._owner_mode_states_guard:
            return self._owner_mode_states.setdefault(account_id, _OwnerModeState())

    @staticmethod
    def _try_acquire_owner_mode_locked(
        state: _OwnerModeState,
        requested_mode: str,
    ) -> Tuple[bool, Optional[str]]:
        if state.mode is None:
            state.mode = requested_mode
            state.active_count = 1
            return True, None
        if state.mode == requested_mode:
            state.active_count += 1
            return True, None
        if state.active_count == 0:
            previous_mode = state.mode
            state.mode = requested_mode
            state.active_count = 1
            return True, previous_mode
        return False, None

    def _try_acquire_owner_mode_once_sync(
        self,
        account_id: str,
        requested_mode: str,
    ) -> Tuple[bool, Optional[str]]:
        state = self._get_owner_mode_state(account_id)
        with state.condition:
            return self._try_acquire_owner_mode_locked(state, requested_mode)

    def _acquire_owner_mode_sync(self, account_id: str, requested_mode: str) -> Optional[str]:
        state = self._get_owner_mode_state(account_id)
        with state.condition:
            while True:
                acquired, previous_mode = self._try_acquire_owner_mode_locked(
                    state,
                    requested_mode,
                )
                if acquired:
                    return previous_mode
                state.condition.wait()

    async def _acquire_owner_mode_async(self, account_id: str, requested_mode: str) -> Optional[str]:
        while True:
            acquire_task = asyncio.create_task(
                asyncio.to_thread(
                    self._try_acquire_owner_mode_once_sync,
                    account_id,
                    requested_mode,
                )
            )
            try:
                acquired, previous_mode = await asyncio.shield(acquire_task)
            except asyncio.CancelledError:
                try:
                    acquired, _previous_mode = await acquire_task
                except Exception:
                    acquired = False
                if acquired:
                    self._release_owner_mode(account_id, requested_mode)
                raise
            if acquired:
                return previous_mode
            await asyncio.sleep(0.05)

    def _release_owner_mode(self, account_id: str, requested_mode: str) -> None:
        state = self._get_owner_mode_state(account_id)
        with state.condition:
            if state.mode != requested_mode:
                return
            if state.active_count > 0:
                state.active_count -= 1
            if state.active_count == 0:
                state.condition.notify_all()

    def _reset_owner_mode(self, account_id: str) -> None:
        state = self._get_owner_mode_state(account_id)
        with state.condition:
            state.mode = None
            state.active_count = 0
            state.condition.notify_all()

    def _reset_owner_mode_if_matches(self, account_id: str, expected_mode: str) -> None:
        state = self._get_owner_mode_state(account_id)
        with state.condition:
            if state.mode != expected_mode:
                return
            state.mode = None
            state.active_count = 0
            state.condition.notify_all()

    def _invalidate_async_runtime_blocking(self, account_id: str, *, reason: str) -> bool:
        async def _invalidate():
            return await self.invalidate_runtime(account_id, reason=reason)

        result_box: Dict[str, Any] = {}
        error_box: Dict[str, BaseException] = {}
        done = threading.Event()

        def _runner() -> None:
            try:
                result_box["value"] = asyncio.run(_invalidate())
            except BaseException as exc:  # noqa: BLE001
                error_box["error"] = exc
            finally:
                done.set()

        thread = threading.Thread(
            target=_runner,
            name=f"account-browser-invalidate-async-{account_id}",
            daemon=True,
        )
        thread.start()
        done.wait()
        if "error" in error_box:
            raise error_box["error"]
        return bool(result_box.get("value"))

    def resolve_profile_dir(self, account_id: str) -> str:
        account_id = self._normalize_account_id(account_id)
        browser_data_dir = (Path(self.base_dir) / "browser_data").resolve()
        profile_dir = (browser_data_dir / f"user_{account_id}").resolve()
        if browser_data_dir != profile_dir and browser_data_dir not in profile_dir.parents:
            raise ValueError("account_id 解析出的 profile_dir 超出 browser_data 目录")
        profile_dir.mkdir(parents=True, exist_ok=True)
        return str(profile_dir)

    async def _close_async_runtime(self, runtime: Any, *, reason: str) -> None:
        if runtime is not None and self.runtime_closer is not None:
            await self.runtime_closer(runtime, reason=reason)

    async def _close_async_page_shielded(self, page: Any) -> bool:
        close_task = asyncio.create_task(_close_async_page(page))
        try:
            await asyncio.shield(close_task)
            return False
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(close_task, timeout=0.5)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                close_task.cancel()
                try:
                    await close_task
                except asyncio.CancelledError:
                    pass
                except Exception as close_error:
                    logger.debug(
                        f"[runtime-manager#{self._manager_instance_id}] async page close failed after forced cancellation: "
                        f"error={close_error}"
                    )
                logger.debug(
                    f"[runtime-manager#{self._manager_instance_id}] async page close did not stop promptly after cancellation"
                )
            except Exception as close_error:
                logger.debug(
                    f"[runtime-manager#{self._manager_instance_id}] async page close failed after cancellation: "
                    f"error={close_error}"
                )
            return True

    async def _create_async_page_shielded(self, page_awaitable: Any) -> Tuple[Any, bool]:
        create_task = asyncio.ensure_future(page_awaitable)
        try:
            return await asyncio.shield(create_task), False
        except asyncio.CancelledError:
            was_cancelled = bool(asyncio.current_task() and asyncio.current_task().cancelling())
            try:
                page = await create_task
            except asyncio.CancelledError:
                raise
            except Exception as create_error:
                if was_cancelled:
                    logger.debug(
                        f"[runtime-manager#{self._manager_instance_id}] async page creation failed after cancellation: "
                        f"error={create_error}"
                    )
                    raise asyncio.CancelledError() from create_error
                raise
            return page, True

    async def _close_async_runtime_shielded(
        self,
        runtime: Any,
        *,
        reason: str,
    ) -> bool:
        close_task = asyncio.create_task(
            self._close_async_runtime(runtime, reason=reason)
        )
        try:
            await asyncio.shield(close_task)
            return False
        except asyncio.CancelledError:
            try:
                await close_task
            except asyncio.CancelledError:
                pass
            except Exception as close_error:
                logger.warning(
                    f"[runtime-manager#{self._manager_instance_id}] async runtime close failed after cancellation: "
                    f"reason={reason}, error={close_error}"
                )
            finally:
                pass
            return True

    def _close_sync_runtime(self, runtime: Any, *, reason: str) -> None:
        if runtime is not None:
            self.sync_runtime_closer(runtime, reason=reason)

    def _should_close_sync_runtime_on_account_worker(
        self,
        account_id: str,
        runtime: Any,
        *,
        owner_thread_id: Optional[int],
        current_thread_id: int,
    ) -> bool:
        return bool(
            runtime is not None
            and owner_thread_id is not None
            and owner_thread_id != current_thread_id
            and not getattr(runtime, "cdp_endpoint", None)
            and self.is_sync_account_worker_thread(account_id, thread_id=owner_thread_id)
        )

    def _close_sync_runtime_on_owner_thread_if_needed(
        self,
        account_id: str,
        runtime: Any,
        *,
        reason: str,
        owner_thread_id: Optional[int],
        current_thread_id: int,
    ) -> None:
        if self._should_close_sync_runtime_on_account_worker(
            account_id,
            runtime,
            owner_thread_id=owner_thread_id,
            current_thread_id=current_thread_id,
        ):
            self.run_sync_task_on_account_thread(
                account_id,
                lambda runtime_to_close=runtime: self._close_sync_runtime(
                    runtime_to_close,
                    reason=reason,
                ),
            )
            return
        self._close_sync_runtime(runtime, reason=reason)

    @staticmethod
    def _take_sync_closures_for_thread(
        state: _SyncRuntimeState,
        current_thread_id: int,
    ) -> list[tuple[Any, str]]:
        if state.active_leases != 0 or not state.pending_closures:
            return []
        closures_to_run = []
        remaining = []
        for runtime, reason, owner_thread_id in state.pending_closures:
            if (
                owner_thread_id is None
                or owner_thread_id == current_thread_id
                or not _thread_id_is_alive(owner_thread_id)
            ):
                closures_to_run.append((runtime, reason))
            else:
                remaining.append((runtime, reason, owner_thread_id))
        state.pending_closures = remaining
        return closures_to_run

    @staticmethod
    def _defer_or_close_sync_runtime(
        state: _SyncRuntimeState,
        runtime: Any,
        *,
        reason: str,
        owner_thread_id: Optional[int],
        current_thread_id: int,
    ) -> list[tuple[Any, str]]:
        if runtime is None:
            return []
        if owner_thread_id is None or owner_thread_id == current_thread_id:
            return [(runtime, reason)]
        if getattr(runtime, "cdp_endpoint", None):
            # Managed CDP runtimes can be force-closed across threads: even if
            # Browser.close()/playwright.stop() reject cross-thread access, the
            # closer still falls back to terminating the underlying process.
            #
            # This is important for flows like:
            # - main thread: password login managed runtime
            # - worker thread: token_refresh_slider managed runtime
            #
            # If we defer closure until the original owner thread cleans up, we
            # may launch a second Chromium against the same profile first, and
            # headless CloakBrowser can then fail with
            # "DevToolsActivePort was ready" / startup collisions.
            return [(runtime, reason)]
        state.pending_closures.append((runtime, reason, owner_thread_id))
        return []

    async def _ensure_async_runtime(
        self,
        account_id: str,
        state: _RuntimeState,
        purpose: str,
        exclusive: bool,
        runtime_request: Optional[Dict[str, Any]] = None,
    ) -> Any:
        resolved_profile_dir = self.resolve_profile_dir(account_id)
        profile_dir = _resolve_runtime_profile_dir(resolved_profile_dir, runtime_request)
        profile_dir = self._ensure_profile_claim(
            state,
            account_id=account_id,
            profile_dir=profile_dir,
            mode="async",
            purpose=purpose,
            thread_id=threading.get_ident(),
        )
        runtime_identity = _build_runtime_identity(
            profile_dir,
            runtime_request,
            default_persistent_context=True,
        )
        if _runtime_is_alive(state.runtime):
            if state.runtime_identity is None or state.runtime_identity == runtime_identity:
                return state.runtime
            if state.active_leases > 0:
                raise ValueError("同账号已存在不兼容的 async runtime 正在使用中，请稍后重试")
        stale_runtime = state.runtime
        if stale_runtime is not None:
            state.runtime = None
            state.runtime_identity = None
            state.current_purpose = None
            state.generation += 1
            close_cancelled = False
            try:
                close_cancelled = await self._close_async_runtime_shielded(
                    stale_runtime,
                    reason="stale_runtime",
                )
            except (Exception, asyncio.CancelledError):
                if state.runtime is None:
                    self._release_profile_claim(state)
                raise
            if close_cancelled:
                if state.runtime is None:
                    self._release_profile_claim(state)
                raise asyncio.CancelledError()
        try:
            runtime = await _call_runtime_factory(
                self.runtime_factory,
                account_id,
                profile_dir,
                state.generation,
                purpose,
                exclusive,
                runtime_request=runtime_request,
            )
        except (Exception, asyncio.CancelledError):
            if state.runtime is None:
                self._release_profile_claim(state)
            raise
        state.runtime = runtime
        state.runtime_identity = runtime_identity
        state.current_purpose = _normalize_runtime_purpose(purpose)
        return runtime

    async def acquire_runtime(
        self,
        account_id: str,
        purpose: str,
        *,
        exclusive: bool,
        runtime_request: Optional[Dict[str, Any]] = None,
    ) -> AccountBrowserRuntimeLease:
        account_id = self._normalize_account_id(account_id)
        normalized_purpose = _normalize_runtime_purpose(purpose)
        request_thread_id = threading.get_ident()
        previous_mode = await self._acquire_owner_mode_async(account_id, "async")
        logger.info(
            f"[runtime-manager#{self._manager_instance_id}] async acquire requested: "
            f"account_id={account_id}, purpose={normalized_purpose}, exclusive={bool(exclusive)}, "
            f"thread_id={request_thread_id}"
        )
        try:
            if previous_mode == "sync":
                self.invalidate_runtime_sync(
                    account_id,
                    reason="owner_mode_switch_to_async",
                )
            state = self._states.setdefault(account_id, _RuntimeState())
            async with state.condition:
                self._raise_if_runtime_draining_pending_closures(
                    state,
                    account_id=account_id,
                    mode="async",
                )
                while state.active_leases and (exclusive or state.active_exclusive):
                    await state.condition.wait()
                    self._raise_if_runtime_draining_pending_closures(
                        state,
                        account_id=account_id,
                        mode="async",
                    )
                runtime = await self._ensure_async_runtime(
                    account_id,
                    state,
                    purpose,
                    exclusive,
                    runtime_request=runtime_request,
                )
                state.active_leases += 1
                state.active_exclusive = bool(exclusive)
                return AccountBrowserRuntimeLease(
                    account_id=account_id,
                    purpose=normalized_purpose,
                    exclusive=exclusive,
                    generation=state.generation,
                    profile_dir=self.resolve_profile_dir(account_id),
                    runtime=runtime,
                )
        except (Exception, asyncio.CancelledError):
            self._release_owner_mode(account_id, "async")
            raise

    async def release_runtime(
        self,
        lease: Optional[AccountBrowserRuntimeLease],
        *,
        reason: str = "released",
    ) -> None:
        if lease is None or lease.released:
            return
        normalized_reason = str(reason or "released")
        logger.info(
            f"[runtime-manager#{self._manager_instance_id}] async release requested: "
            f"account_id={lease.account_id}, purpose={_normalize_runtime_purpose(lease.purpose)}, "
            f"reason={normalized_reason}, thread_id={threading.get_ident()}"
        )
        state = self._states.get(lease.account_id)
        if state is None:
            lease.released = True
            self._release_owner_mode(lease.account_id, "async")
            return
        runtime = lease.runtime
        pages_to_close = [
            page for page in list(lease.pages)
            if not _should_preserve_managed_runtime_page(runtime, page)
        ]
        lease.pages.clear()
        for page in pages_to_close:
            if getattr(runtime, "page", None) is page:
                runtime.page = None
        closures_to_run = []
        should_release_claim = False
        async with state.condition:
            if state.active_leases > 0:
                state.active_leases -= 1
            if state.active_leases == 0:
                state.active_exclusive = False
                state.last_released_at = self.time_fn()
                closures_to_run = list(state.pending_closures)
                state.pending_closures.clear()
                should_release_claim = state.runtime is None
                if state.runtime is None:
                    state.current_purpose = None
            lease.released = True
            state.condition.notify_all()
        close_errors = []
        release_cancelled = False
        try:
            for page in pages_to_close:
                try:
                    if await self._close_async_page_shielded(page):
                        release_cancelled = True
                except asyncio.CancelledError:
                    if closures_to_run:
                        release_cancelled = True
                        break
                    raise
                except Exception:
                    pass
            for runtime, close_reason in closures_to_run:
                try:
                    if await self._close_async_runtime_shielded(runtime, reason=close_reason):
                        release_cancelled = True
                except asyncio.CancelledError:
                    release_cancelled = True
                except Exception as close_error:
                    close_errors.append(close_error)
        finally:
            if should_release_claim:
                self._release_profile_claim(state)
            self._release_owner_mode(lease.account_id, "async")
        logger.info(
            f"[runtime-manager#{self._manager_instance_id}] async release completed: "
            f"account_id={lease.account_id}, purpose={_normalize_runtime_purpose(lease.purpose)}, "
            f"reason={normalized_reason}, active_leases={getattr(state, 'active_leases', 'unknown')}, "
            f"thread_id={threading.get_ident()}"
        )
        if close_errors:
            first_error = close_errors[0]
            process_lookup_errors = [err for err in close_errors if isinstance(err, ProcessLookupError)]
            if len(process_lookup_errors) == len(close_errors):
                return
            raise first_error
        if release_cancelled:
            raise asyncio.CancelledError()

    async def get_fresh_page(self, lease: AccountBrowserRuntimeLease) -> Tuple[Any, Any]:
        if lease.released:
            raise RuntimeError("runtime lease has already been released")
        runtime = lease.runtime
        context = getattr(runtime, "context", None)
        if context is None:
            raise RuntimeError("runtime context is unavailable")
        reused_page = _reuse_runtime_page_if_available(runtime, context, lease.pages)
        if reused_page is not None:
            lease.pages.append(reused_page)
            return reused_page, context
        new_page = getattr(context, "new_page", None)
        if not callable(new_page):
            raise RuntimeError("runtime context cannot create pages")
        page = new_page()
        page_create_cancelled = False
        if inspect.isawaitable(page):
            page, page_create_cancelled = await self._create_async_page_shielded(page)
        runtime.page = page
        lease.pages.append(page)
        if page_create_cancelled:
            raise asyncio.CancelledError()
        return page, context

    async def invalidate_runtime(self, account_id: str, *, reason: str = "invalidated") -> bool:
        account_id = self._normalize_account_id(account_id)
        normalized_reason = str(reason or "invalidated")
        logger.info(
            f"[runtime-manager#{self._manager_instance_id}] async invalidate requested: "
            f"account_id={account_id}, reason={normalized_reason}, thread_id={threading.get_ident()}"
        )
        state = self._states.setdefault(account_id, _RuntimeState())
        should_release_claim = False
        async with state.condition:
            runtime = state.runtime
            if runtime is None:
                logger.info(
                    f"[runtime-manager#{self._manager_instance_id}] async invalidate skipped: "
                    f"account_id={account_id}, reason={normalized_reason}, runtime=missing"
                )
                return False
            state.runtime = None
            state.runtime_identity = None
            state.generation += 1
            if state.active_leases > 0:
                state.pending_closures.append((runtime, reason))
                logger.info(
                    f"[runtime-manager#{self._manager_instance_id}] async invalidate deferred: "
                    f"account_id={account_id}, reason={normalized_reason}, active_leases={state.active_leases}"
                )
                return True
            should_release_claim = True
        close_error = None
        close_cancelled = False
        try:
            close_cancelled = await self._close_async_runtime_shielded(runtime, reason=reason)
        except Exception as error:
            close_error = error
        finally:
            if should_release_claim:
                self._release_profile_claim(state)
                self._reset_owner_mode_if_matches(account_id, "async")
        logger.info(
            f"[runtime-manager#{self._manager_instance_id}] async invalidate completed: "
            f"account_id={account_id}, reason={normalized_reason}, thread_id={threading.get_ident()}"
        )
        if close_error is not None:
            raise close_error
        if close_cancelled:
            raise asyncio.CancelledError()
        return True

    async def cleanup_idle_runtimes(self) -> int:
        closed_count = 0
        close_errors = []
        for account_id, state in list(self._states.items()):
            runtime_to_close = None
            should_release_claim = False
            async with state.condition:
                is_idle = (
                    state.runtime is not None
                    and state.active_leases == 0
                    and (self.time_fn() - state.last_released_at) >= self.idle_timeout_seconds
                )
                if is_idle:
                    runtime_to_close = state.runtime
                    state.runtime = None
                    state.runtime_identity = None
                    state.generation += 1
                    should_release_claim = True
            if runtime_to_close is None:
                continue
            close_error = None
            close_cancelled = False
            try:
                close_cancelled = await self._close_async_runtime_shielded(
                    runtime_to_close,
                    reason="idle_timeout",
                )
                closed_count += 1
            except Exception as error:
                close_error = error
            finally:
                if should_release_claim:
                    self._release_profile_claim(state)
                    self._reset_owner_mode_if_matches(account_id, "async")
            if close_error is not None:
                close_errors.append(close_error)
            if close_cancelled:
                raise asyncio.CancelledError()
        if close_errors:
            raise close_errors[0]
        return closed_count

    def _ensure_sync_runtime(
        self,
        account_id: str,
        state: _SyncRuntimeState,
        purpose: str,
        exclusive: bool,
        runtime_request: Optional[Dict[str, Any]] = None,
    ) -> Any:
        current_thread_id = threading.get_ident()
        resolved_profile_dir = self.resolve_profile_dir(account_id)
        profile_dir = _resolve_runtime_profile_dir(resolved_profile_dir, runtime_request)
        profile_dir = self._ensure_profile_claim(
            state,
            account_id=account_id,
            profile_dir=profile_dir,
            mode="sync",
            purpose=purpose,
            thread_id=current_thread_id,
        )
        runtime_identity = _build_runtime_identity(
            profile_dir,
            runtime_request,
            default_persistent_context=False,
        )
        if _runtime_is_alive(state.runtime) and state.owner_thread_id == current_thread_id:
            if state.runtime_identity is None or state.runtime_identity == runtime_identity:
                return state.runtime
            if state.active_leases > 0:
                raise ValueError("同账号已存在不兼容的 sync runtime 正在使用中，请稍后重试")
        stale_runtime = state.runtime
        if stale_runtime is not None:
            stale_owner_thread_id = state.owner_thread_id
            stale_reason = (
                "thread_changed"
                if _runtime_is_alive(stale_runtime) and stale_owner_thread_id != current_thread_id
                else "stale_runtime"
            )
            state.runtime = None
            state.runtime_identity = None
            state.current_purpose = None
            state.owner_thread_id = None
            state.generation += 1
            if self._should_close_sync_runtime_on_account_worker(
                account_id,
                stale_runtime,
                owner_thread_id=stale_owner_thread_id,
                current_thread_id=current_thread_id,
            ):
                closures_to_run = [(stale_runtime, stale_reason)]
            else:
                closures_to_run = self._defer_or_close_sync_runtime(
                    state,
                    stale_runtime,
                    reason=stale_reason,
                    owner_thread_id=stale_owner_thread_id,
                    current_thread_id=current_thread_id,
                )
            try:
                for runtime, close_reason in closures_to_run:
                    self._close_sync_runtime_on_owner_thread_if_needed(
                        account_id,
                        runtime,
                        reason=close_reason,
                        owner_thread_id=stale_owner_thread_id,
                        current_thread_id=current_thread_id,
                    )
            except Exception:
                if state.runtime is None:
                    self._release_profile_claim(state)
                raise
        try:
            runtime = _call_runtime_factory(
                self.sync_runtime_factory,
                account_id,
                profile_dir,
                state.generation,
                purpose,
                exclusive,
                runtime_request=runtime_request,
            )
        except Exception:
            if state.runtime is None:
                self._release_profile_claim(state)
            raise
        state.runtime = runtime
        state.runtime_identity = runtime_identity
        state.current_purpose = _normalize_runtime_purpose(purpose)
        state.owner_thread_id = current_thread_id
        return runtime

    def acquire_runtime_sync(
        self,
        account_id: str,
        purpose: str,
        *,
        exclusive: bool,
        runtime_request: Optional[Dict[str, Any]] = None,
    ) -> SyncAccountBrowserRuntimeLease:
        account_id = self._normalize_account_id(account_id)
        normalized_purpose = _normalize_runtime_purpose(purpose)
        with self._sync_states_guard:
            state = self._sync_states.setdefault(account_id, _SyncRuntimeState())
        current_thread_id = threading.get_ident()
        previous_mode = self._acquire_owner_mode_sync(account_id, "sync")
        logger.info(
            f"[runtime-manager#{self._manager_instance_id}] sync acquire requested: "
            f"account_id={account_id}, purpose={normalized_purpose}, exclusive={bool(exclusive)}, "
            f"thread_id={current_thread_id}"
        )
        try:
            if previous_mode == "async":
                self._invalidate_async_runtime_blocking(
                    account_id,
                    reason="owner_mode_switch_to_sync",
                )
            closures_to_run = []
            with state.condition:
                self._raise_if_runtime_draining_pending_closures(
                    state,
                    account_id=account_id,
                    mode="sync",
                )
                can_reenter_current_runtime = bool(
                    state.active_leases
                    and state.owner_thread_id == current_thread_id
                    and _runtime_is_alive(state.runtime)
                )
                while state.active_leases and (
                    exclusive
                    or state.active_exclusive
                    or (
                        state.owner_thread_id is not None
                        and state.owner_thread_id != current_thread_id
                        and _runtime_is_alive(state.runtime)
                    )
                ):
                    if can_reenter_current_runtime:
                        break
                    state.condition.wait()
                    self._raise_if_runtime_draining_pending_closures(
                        state,
                        account_id=account_id,
                        mode="sync",
                    )
                    can_reenter_current_runtime = bool(
                        state.active_leases
                        and state.owner_thread_id == current_thread_id
                        and _runtime_is_alive(state.runtime)
                    )
                closures_to_run = self._take_sync_closures_for_thread(state, current_thread_id)
                runtime = self._ensure_sync_runtime(
                    account_id,
                    state,
                    purpose,
                    exclusive,
                    runtime_request=runtime_request,
                )
                state.active_leases += 1
                state.active_exclusive = bool(exclusive)
                lease = SyncAccountBrowserRuntimeLease(
                    account_id=account_id,
                    purpose=normalized_purpose,
                    exclusive=exclusive,
                    generation=state.generation,
                    profile_dir=self.resolve_profile_dir(account_id),
                    runtime=runtime,
                )
            for runtime_to_close, close_reason in closures_to_run:
                self._close_sync_runtime(runtime_to_close, reason=close_reason)
            logger.info(
                f"[runtime-manager#{self._manager_instance_id}] sync acquire granted: "
                f"account_id={account_id}, purpose={normalized_purpose}, exclusive={bool(exclusive)}, "
                f"generation={lease.generation}, active_leases={state.active_leases}, "
                f"active_exclusive={state.active_exclusive}, thread_id={current_thread_id}"
            )
            return lease
        except Exception:
            self._release_owner_mode(account_id, "sync")
            raise

    def release_runtime_sync(
        self,
        lease: Optional[SyncAccountBrowserRuntimeLease],
        *,
        reason: str = "released",
    ) -> None:
        if lease is None or lease.released:
            return
        normalized_reason = str(reason or "released")
        current_thread_id = threading.get_ident()
        logger.info(
            f"[runtime-manager#{self._manager_instance_id}] sync release requested: "
            f"account_id={lease.account_id}, purpose={_normalize_runtime_purpose(lease.purpose)}, "
            f"reason={normalized_reason}, thread_id={current_thread_id}"
        )
        state = self._sync_states.get(lease.account_id)
        if state is None:
            lease.released = True
            self._release_owner_mode(lease.account_id, "sync")
            return
        runtime = lease.runtime
        pages_to_close = [
            page for page in list(lease.pages)
            if not _should_preserve_managed_runtime_page(runtime, page)
        ]
        lease.pages.clear()
        for page in pages_to_close:
            try:
                _close_sync_page(page)
            except Exception:
                pass
        closures_to_run = []
        should_release_claim = False
        with state.condition:
            if state.active_leases > 0:
                state.active_leases -= 1
            if state.active_leases == 0:
                state.active_exclusive = False
                state.last_released_at = self.time_fn()
                remaining_pending_closures = []
                for pending_runtime, close_reason, owner_thread_id in state.pending_closures:
                    if pending_runtime is None:
                        continue
                    can_close_now = bool(
                        owner_thread_id is None
                        or owner_thread_id == current_thread_id
                        or not _thread_id_is_alive(owner_thread_id)
                        or self._should_close_sync_runtime_on_account_worker(
                            lease.account_id,
                            pending_runtime,
                            owner_thread_id=owner_thread_id,
                            current_thread_id=current_thread_id,
                        )
                    )
                    if can_close_now:
                        closures_to_run.append((pending_runtime, close_reason, owner_thread_id))
                    else:
                        remaining_pending_closures.append((pending_runtime, close_reason, owner_thread_id))
                state.pending_closures = remaining_pending_closures
                should_release_claim = state.runtime is None and not state.pending_closures
                if state.runtime is None:
                    state.current_purpose = None
            lease.released = True
            state.condition.notify_all()
        close_errors = []
        try:
            for runtime_to_close, close_reason, owner_thread_id in closures_to_run:
                try:
                    self._close_sync_runtime_on_owner_thread_if_needed(
                        lease.account_id,
                        runtime_to_close,
                        reason=close_reason,
                        owner_thread_id=owner_thread_id,
                        current_thread_id=current_thread_id,
                    )
                except Exception as close_error:
                    close_errors.append(close_error)
        finally:
            if should_release_claim:
                self._release_profile_claim(state)
            self._release_owner_mode(lease.account_id, "sync")
            logger.info(
                f"[runtime-manager#{self._manager_instance_id}] sync release completed: "
                f"account_id={lease.account_id}, purpose={_normalize_runtime_purpose(lease.purpose)}, "
                f"reason={normalized_reason}, active_leases={getattr(state, 'active_leases', 'unknown')}, "
                f"thread_id={current_thread_id}"
            )
        if close_errors:
            raise close_errors[0]

    def get_fresh_page_sync(self, lease: SyncAccountBrowserRuntimeLease) -> Tuple[Any, Any]:
        if lease.released:
            raise RuntimeError("runtime lease has already been released")
        runtime = lease.runtime
        context = getattr(runtime, "context", None)
        if context is None:
            raise RuntimeError("runtime context is unavailable")
        closed = _safe_bool_call(context, "is_closed")
        if closed is True:
            raise RuntimeError("runtime context is closed")
        reused_page = _reuse_runtime_page_if_available(runtime, context, lease.pages)
        if reused_page is not None:
            lease.pages.append(reused_page)
            return reused_page, context
        page = context.new_page()
        runtime.page = page
        lease.pages.append(page)
        return page, context

    def invalidate_runtime_sync(self, account_id: str, *, reason: str = "invalidated") -> bool:
        account_id = self._normalize_account_id(account_id)
        normalized_reason = str(reason or "invalidated")
        with self._sync_states_guard:
            state = self._sync_states.setdefault(account_id, _SyncRuntimeState())
        current_thread_id = threading.get_ident()
        logger.info(
            f"[runtime-manager#{self._manager_instance_id}] sync invalidate requested: "
            f"account_id={account_id}, reason={normalized_reason}, thread_id={current_thread_id}"
        )
        closures_to_run = []
        should_release_claim = False
        with state.condition:
            runtime = state.runtime
            owner_thread_id = state.owner_thread_id
            if runtime is None:
                logger.info(
                    f"[runtime-manager#{self._manager_instance_id}] sync invalidate skipped: "
                    f"account_id={account_id}, reason={normalized_reason}, runtime=missing"
                )
                return False
            state.runtime = None
            state.runtime_identity = None
            state.owner_thread_id = None
            state.generation += 1
            if state.active_leases > 0:
                if getattr(runtime, "cdp_endpoint", None):
                    closures_to_run.extend(
                        self._defer_or_close_sync_runtime(
                            state,
                            runtime,
                            reason=reason,
                            owner_thread_id=owner_thread_id,
                            current_thread_id=current_thread_id,
                        )
                    )
                    # Runtime 已被强制关闭，但旧 lease 还没释放；用空闭包作 draining 哨兵，
                    # 阻止同账号提前重建，release_runtime_sync() 会在 active_leases 归零时清掉它。
                    state.pending_closures.append((None, reason, None))
                    logger.info(
                        f"[runtime-manager#{self._manager_instance_id}] sync invalidate force-closing managed runtime: "
                        f"account_id={account_id}, reason={normalized_reason}, active_leases={state.active_leases}, "
                        f"owner_thread_id={owner_thread_id}"
                    )
                else:
                    state.pending_closures.append((runtime, reason, owner_thread_id))
                    logger.info(
                        f"[runtime-manager#{self._manager_instance_id}] sync invalidate deferred: "
                        f"account_id={account_id}, reason={normalized_reason}, active_leases={state.active_leases}, "
                        f"owner_thread_id={owner_thread_id}"
                    )
            else:
                closures_to_run.extend(
                    self._defer_or_close_sync_runtime(
                        state,
                        runtime,
                        reason=reason,
                        owner_thread_id=owner_thread_id,
                        current_thread_id=current_thread_id,
                    )
                )
                closures_to_run.extend(self._take_sync_closures_for_thread(state, current_thread_id))
                should_release_claim = True
        close_errors = []
        try:
            for runtime_to_close, close_reason in closures_to_run:
                try:
                    self._close_sync_runtime(runtime_to_close, reason=close_reason)
                except Exception as close_error:
                    close_errors.append(close_error)
        finally:
            if should_release_claim:
                self._release_profile_claim(state)
                self._reset_owner_mode_if_matches(account_id, "sync")
        logger.info(
            f"[runtime-manager#{self._manager_instance_id}] sync invalidate completed: "
            f"account_id={account_id}, reason={normalized_reason}, thread_id={current_thread_id}"
        )
        if close_errors:
            raise close_errors[0]
        return True

    async def close_all_runtimes(self, *, reason: str = "shutdown") -> Dict[str, int]:
        async_runtimes_to_close = []
        async_states_to_release = []
        async_states_snapshot = list(self._states.items())

        for account_id, state in async_states_snapshot:
            should_release_claim = False
            async with state.condition:
                runtime = state.runtime
                pending_closures = list(state.pending_closures)
                if runtime is None and not pending_closures:
                    continue
                state.runtime = None
                state.runtime_identity = None
                state.active_leases = 0
                state.active_exclusive = False
                state.pending_closures.clear()
                state.generation += 1
                state.last_released_at = self.time_fn()
                state.condition.notify_all()
                async_runtimes_to_close.extend(
                    [(account_id, state, runtime)] if runtime is not None else []
                )
                async_runtimes_to_close.extend(
                    (account_id, state, pending_runtime)
                    for pending_runtime, _close_reason in pending_closures
                    if pending_runtime is not None
                )
                should_release_claim = True
            if should_release_claim:
                async_states_to_release.append((account_id, state))

        closed_async = 0
        seen_async_runtime_ids = set()
        close_errors = []
        close_cancelled = False
        try:
            for _account_id, _state, runtime in async_runtimes_to_close:
                runtime_id = id(runtime)
                if runtime is None or runtime_id in seen_async_runtime_ids:
                    continue
                seen_async_runtime_ids.add(runtime_id)
                try:
                    if await self._close_async_runtime_shielded(runtime, reason=reason):
                        close_cancelled = True
                    closed_async += 1
                except asyncio.CancelledError:
                    close_cancelled = True
                except Exception as close_error:
                    close_errors.append(close_error)
        finally:
            released_state_ids = set()
            for account_id, state in async_states_to_release:
                state_id = id(state)
                if state_id in released_state_ids:
                    continue
                released_state_ids.add(state_id)
                self._release_profile_claim(state)
                self._reset_owner_mode_if_matches(account_id, "async")

        closed_sync = 0
        try:
            closed_sync = self.close_all_runtimes_sync(reason=reason)
        except Exception as close_error:
            close_errors.append(close_error)
        if close_errors:
            raise close_errors[0]
        if close_cancelled:
            raise asyncio.CancelledError()
        return {
            "async": closed_async,
            "sync": closed_sync,
        }

    def cleanup_idle_runtimes_sync(self) -> int:
        closed_count = 0
        all_close_errors = []
        idle_worker_accounts_to_stop = set()
        current_thread_id = threading.get_ident()
        with self._sync_states_guard:
            states = list(self._sync_states.items())
        for account_id, state in states:
            closures_to_run = []
            should_release_claim = False
            with state.condition:
                is_idle = (
                    state.runtime is not None
                    and state.active_leases == 0
                    and (self.time_fn() - state.last_released_at) >= self.idle_timeout_seconds
                )
                if is_idle:
                    runtime_to_close = state.runtime
                    owner_thread_id = state.owner_thread_id
                    can_close_now = bool(
                        owner_thread_id is None
                        or owner_thread_id == current_thread_id
                        or not _thread_id_is_alive(owner_thread_id)
                        or getattr(runtime_to_close, "cdp_endpoint", None)
                    )
                    if not can_close_now:
                        can_close_on_owner_worker = self._should_close_sync_runtime_on_account_worker(
                            account_id,
                            runtime_to_close,
                            owner_thread_id=owner_thread_id,
                            current_thread_id=current_thread_id,
                        )
                        if not can_close_on_owner_worker:
                            continue
                    state.runtime = None
                    state.runtime_identity = None
                    state.owner_thread_id = None
                    state.current_purpose = None
                    state.generation += 1
                    if can_close_now:
                        closures_to_run.extend(
                            (
                                runtime_to_close,
                                close_reason,
                                owner_thread_id,
                            )
                            for runtime_to_close, close_reason in self._defer_or_close_sync_runtime(
                                state,
                                runtime_to_close,
                                reason="idle_timeout",
                                owner_thread_id=owner_thread_id,
                                current_thread_id=current_thread_id,
                            )
                        )
                    else:
                        closures_to_run.append((runtime_to_close, "idle_timeout", owner_thread_id))
                    should_release_claim = True
                closures_to_run.extend(
                    (runtime_to_close, close_reason, None)
                    for runtime_to_close, close_reason in self._take_sync_closures_for_thread(
                        state,
                        current_thread_id,
                    )
                )
            if not closures_to_run:
                continue
            close_errors = []
            try:
                for runtime_to_close, close_reason, owner_thread_id in closures_to_run:
                    try:
                        self._close_sync_runtime_on_owner_thread_if_needed(
                            account_id,
                            runtime_to_close,
                            reason=close_reason,
                            owner_thread_id=owner_thread_id,
                            current_thread_id=current_thread_id,
                        )
                        closed_count += 1
                    except Exception as close_error:
                        close_errors.append(close_error)
            finally:
                if should_release_claim:
                    self._release_profile_claim(state)
                    self._reset_owner_mode_if_matches(account_id, "sync")
            if close_errors:
                all_close_errors.extend(close_errors)
            elif should_release_claim:
                idle_worker_accounts_to_stop.add(account_id)
        if idle_worker_accounts_to_stop:
            closed_count += self.close_all_account_workers_sync(
                account_ids=idle_worker_accounts_to_stop,
            )
        closed_count += self.cleanup_idle_account_workers_sync()
        if all_close_errors:
            raise all_close_errors[0]
        return closed_count

    def cleanup_idle_account_workers_sync(self) -> int:
        stopped_count = 0
        with self._sync_account_workers_guard:
            workers = list(self._sync_account_workers.items())
        for account_id, state in workers:
            thread_to_join = None
            with state.lock:
                thread = state.thread
                if thread is None or not thread.is_alive():
                    continue
                is_idle = (
                    state.thread_id is not None
                    and state.task_queue.empty()
                    and (self.time_fn() - state.last_used_at) >= self.idle_timeout_seconds
                )
                if not is_idle:
                    continue
                state.stop_requested = True
                thread_to_join = thread
            if thread_to_join is not None:
                thread_to_join.join(timeout=1.0)
                if not thread_to_join.is_alive():
                    stopped_count += 1
                    with self._sync_account_workers_guard:
                        existing_state = self._sync_account_workers.get(account_id)
                        if existing_state is state:
                            self._sync_account_workers.pop(account_id, None)
        return stopped_count

    def close_all_account_workers_sync(
        self,
        *,
        join_timeout: float = 1.0,
        account_ids: Optional[set[str]] = None,
    ) -> int:
        stopped_count = 0
        current_thread_id = threading.get_ident()
        with self._sync_account_workers_guard:
            workers = list(self._sync_account_workers.items())
        for account_id, state in workers:
            if account_ids is not None and account_id not in account_ids:
                continue
            thread_to_join = None
            should_forget_stale_worker = False
            with state.lock:
                thread = state.thread
                if thread is None or not thread.is_alive():
                    should_forget_stale_worker = True
                else:
                    state.stop_requested = True
                    if state.thread_id != current_thread_id:
                        thread_to_join = thread
            if should_forget_stale_worker:
                with self._sync_account_workers_guard:
                    existing_state = self._sync_account_workers.get(account_id)
                    if existing_state is state:
                        self._sync_account_workers.pop(account_id, None)
                continue
            if thread_to_join is None:
                continue
            thread_to_join.join(timeout=max(0.0, float(join_timeout)))
            if not thread_to_join.is_alive():
                stopped_count += 1
                with self._sync_account_workers_guard:
                    existing_state = self._sync_account_workers.get(account_id)
                    if existing_state is state:
                        self._sync_account_workers.pop(account_id, None)
        return stopped_count

    def close_all_runtimes_sync(self, *, reason: str = "shutdown") -> int:
        runtimes_to_close = []
        states_to_release = []
        current_thread_id = threading.get_ident()
        with self._sync_states_guard:
            states_snapshot = list(self._sync_states.items())

        for account_id, state in states_snapshot:
            should_release_claim = False
            with state.condition:
                runtime = state.runtime
                owner_thread_id = state.owner_thread_id
                pending_closures = list(state.pending_closures)
                if runtime is None and not pending_closures:
                    continue
                state.runtime = None
                state.runtime_identity = None
                state.owner_thread_id = None
                state.active_leases = 0
                state.active_exclusive = False
                state.pending_closures.clear()
                state.generation += 1
                state.last_released_at = self.time_fn()
                state.condition.notify_all()
                if runtime is not None:
                    runtimes_to_close.append((account_id, state, runtime, owner_thread_id))
                runtimes_to_close.extend(
                    (account_id, state, pending_runtime, pending_owner_thread_id)
                    for pending_runtime, _close_reason, pending_owner_thread_id in pending_closures
                    if pending_runtime is not None
                )
                should_release_claim = True
            if should_release_claim:
                states_to_release.append((account_id, state))

        closed_count = 0
        seen_runtime_ids = set()
        close_errors = []
        try:
            for account_id, _state, runtime, owner_thread_id in runtimes_to_close:
                runtime_id = id(runtime)
                if runtime_id in seen_runtime_ids:
                    continue
                seen_runtime_ids.add(runtime_id)
                try:
                    self._close_sync_runtime_on_owner_thread_if_needed(
                        account_id,
                        runtime,
                        reason=reason,
                        owner_thread_id=owner_thread_id,
                        current_thread_id=current_thread_id,
                    )
                    closed_count += 1
                except Exception as close_error:
                    close_errors.append(close_error)
        finally:
            released_state_ids = set()
            for account_id, state in states_to_release:
                state_id = id(state)
                if state_id in released_state_ids:
                    continue
                released_state_ids.add(state_id)
                self._release_profile_claim(state)
                self._reset_owner_mode_if_matches(account_id, "sync")
            self.close_all_account_workers_sync()
        if close_errors:
            raise close_errors[0]
        return closed_count

    def get_account_runtime_state_snapshot(self, account_id: str) -> Dict[str, Any]:
        account_id = self._normalize_account_id(account_id)

        owner_mode_state = self._get_owner_mode_state(account_id)
        with owner_mode_state.condition:
            owner_mode = owner_mode_state.mode
            owner_mode_active_count = owner_mode_state.active_count

        async_state = self._states.get(account_id)
        async_snapshot = {
            "runtime_exists": False,
            "runtime_alive": False,
            "current_purpose": None,
            "active_leases": 0,
            "active_exclusive": False,
            "pending_closures": 0,
            "claimed_profile_dir": None,
        }
        if async_state is not None:
            async_snapshot = {
                "runtime_exists": async_state.runtime is not None,
                "runtime_alive": _runtime_is_alive(async_state.runtime),
                "current_purpose": _normalize_runtime_purpose(getattr(async_state, "current_purpose", None)),
                "active_leases": int(getattr(async_state, "active_leases", 0) or 0),
                "active_exclusive": bool(getattr(async_state, "active_exclusive", False)),
                "pending_closures": len(getattr(async_state, "pending_closures", []) or []),
                "claimed_profile_dir": getattr(async_state, "claimed_profile_dir", None),
            }

        with self._sync_states_guard:
            sync_state = self._sync_states.get(account_id)
        sync_snapshot = {
            "runtime_exists": False,
            "runtime_alive": False,
            "current_purpose": None,
            "active_leases": 0,
            "active_exclusive": False,
            "pending_closures": 0,
            "claimed_profile_dir": None,
            "owner_thread_id": None,
        }
        if sync_state is not None:
            with sync_state.condition:
                sync_snapshot = {
                    "runtime_exists": sync_state.runtime is not None,
                    "runtime_alive": _runtime_is_alive(sync_state.runtime),
                    "current_purpose": _normalize_runtime_purpose(getattr(sync_state, "current_purpose", None)),
                    "active_leases": int(getattr(sync_state, "active_leases", 0) or 0),
                    "active_exclusive": bool(getattr(sync_state, "active_exclusive", False)),
                    "pending_closures": len(getattr(sync_state, "pending_closures", []) or []),
                    "claimed_profile_dir": getattr(sync_state, "claimed_profile_dir", None),
                    "owner_thread_id": getattr(sync_state, "owner_thread_id", None),
                }

        return {
            "account_id": account_id,
            "owner_mode": owner_mode,
            "owner_mode_active_count": owner_mode_active_count,
            "async_runtime_exists": async_snapshot["runtime_exists"],
            "async_runtime_alive": async_snapshot["runtime_alive"],
            "async_current_purpose": async_snapshot["current_purpose"],
            "async_active_leases": async_snapshot["active_leases"],
            "async_active_exclusive": async_snapshot["active_exclusive"],
            "async_pending_closures": async_snapshot["pending_closures"],
            "async_claimed_profile_dir": async_snapshot["claimed_profile_dir"],
            "sync_runtime_exists": sync_snapshot["runtime_exists"],
            "sync_runtime_alive": sync_snapshot["runtime_alive"],
            "sync_current_purpose": sync_snapshot["current_purpose"],
            "sync_active_leases": sync_snapshot["active_leases"],
            "sync_active_exclusive": sync_snapshot["active_exclusive"],
            "sync_pending_closures": sync_snapshot["pending_closures"],
            "sync_claimed_profile_dir": sync_snapshot["claimed_profile_dir"],
            "sync_owner_thread_id": sync_snapshot["owner_thread_id"],
        }


account_browser_runtime_manager = AccountBrowserRuntimeManager()
