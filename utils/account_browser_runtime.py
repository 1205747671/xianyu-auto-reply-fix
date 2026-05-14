from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

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
    runtime_identity: Optional[Tuple[str, bool]] = None
    claimed_profile_dir: Optional[str] = None
    claim_owner: Optional[Tuple[int, str, str]] = None
    active_leases: int = 0
    active_exclusive: bool = False
    last_released_at: float = 0.0
    pending_closures: list[tuple[Any, str]] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)


@dataclass
class _SyncRuntimeState:
    generation: int = 0
    runtime: Any = None
    runtime_identity: Optional[Tuple[str, bool]] = None
    claimed_profile_dir: Optional[str] = None
    claim_owner: Optional[Tuple[int, str, str]] = None
    owner_thread_id: Optional[int] = None
    active_leases: int = 0
    active_exclusive: bool = False
    last_released_at: float = 0.0
    pending_closures: list[tuple[Any, str, Optional[int]]] = field(default_factory=list)
    condition: threading.Condition = field(default_factory=threading.Condition)


@dataclass
class _ProfileClaimState:
    owner: Optional[Tuple[int, str, str]] = None


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
    is_alive = getattr(runtime, "is_alive", None)
    if callable(is_alive):
        try:
            return bool(is_alive())
        except Exception:
            return False
    browser = getattr(runtime, "browser", None)
    if browser is not None:
        connected = _safe_bool_call(browser, "is_connected")
        if connected is False:
            return False
    context = getattr(runtime, "context", None)
    if context is not None:
        closed = _safe_bool_call(context, "is_closed")
        if closed is True:
            return False
    page = getattr(runtime, "page", None)
    if page is not None:
        closed = _safe_bool_call(page, "is_closed")
        if closed is True:
            runtime.page = None
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


def _format_profile_claim_owner(owner: Optional[Tuple[int, str, str]]) -> str:
    if owner is None:
        return "unknown-owner"
    manager_instance_id, account_id, mode = owner
    return f"manager={manager_instance_id}, account_id={account_id}, mode={mode}"


def _claim_profile_dir(profile_dir: str, owner: Tuple[int, str, str]) -> str:
    profile_dir_key = _normalize_profile_dir_key(profile_dir)
    with _PROFILE_CLAIMS_GUARD:
        claim_state = _PROFILE_CLAIMS.setdefault(profile_dir_key, _ProfileClaimState())
        if claim_state.owner is None:
            claim_state.owner = owner
            return profile_dir_key
        if claim_state.owner == owner:
            return profile_dir_key
        raise RuntimeError(
            "账号级 browser profile 已被其他 runtime 持有，拒绝并发复用: "
            f"profile_dir={profile_dir_key}, owner={_format_profile_claim_owner(claim_state.owner)}"
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
    return runtime


async def _default_async_runtime_closer(runtime: Any, *, reason: str) -> Any:
    _ = reason
    if getattr(runtime, "cdp_endpoint", None) and callable(close_managed_runtime_handle_async):
        return await _maybe_await(close_managed_runtime_handle_async(runtime, reason=reason))
    page = getattr(runtime, "page", None)
    context = getattr(runtime, "context", None)
    browser = getattr(runtime, "browser", None)
    playwright = getattr(runtime, "playwright", None)

    try:
        if page is not None:
            await _maybe_await(page.close())
    except Exception:
        pass
    try:
        if context is not None:
            await _maybe_await(context.close())
    except Exception:
        pass
    try:
        if browser is not None:
            await _maybe_await(browser.close())
    except Exception:
        pass
    try:
        if playwright is not None:
            await _maybe_await(playwright.stop())
    except Exception:
        pass
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
    use_persistent_context = bool(request.get("use_persistent_context"))
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
) -> Tuple[str, bool]:
    request = dict(runtime_request or {})
    use_persistent_context = request.get("use_persistent_context")
    if use_persistent_context is None:
        use_persistent_context = default_persistent_context
    else:
        use_persistent_context = bool(use_persistent_context)
    return (profile_dir, use_persistent_context)


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
    ) -> str:
        owner = self._build_profile_claim_owner(account_id, mode=mode)
        if state.claimed_profile_dir == profile_dir and state.claim_owner == owner:
            return profile_dir
        claimed_profile_dir = _claim_profile_dir(profile_dir, owner)
        state.claimed_profile_dir = claimed_profile_dir
        state.claim_owner = owner
        return claimed_profile_dir

    @staticmethod
    def _release_profile_claim(state: Any) -> None:
        _release_profile_dir(getattr(state, "claimed_profile_dir", None), getattr(state, "claim_owner", None))
        state.claimed_profile_dir = None
        state.claim_owner = None

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

    def _close_sync_runtime(self, runtime: Any, *, reason: str) -> None:
        if runtime is not None:
            self.sync_runtime_closer(runtime, reason=reason)

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
        )
        runtime_identity = _build_runtime_identity(
            profile_dir,
            runtime_request,
            default_persistent_context=True,
        )
        if _runtime_is_alive(state.runtime):
            if state.runtime_identity is not None and state.runtime_identity != runtime_identity:
                raise ValueError("同账号已存在不兼容的 async runtime，请先失效再重建")
            return state.runtime
        stale_runtime = state.runtime
        if stale_runtime is not None:
            state.runtime = None
            state.runtime_identity = None
            state.generation += 1
            await self._close_async_runtime(stale_runtime, reason="stale_runtime")
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
        except Exception:
            if state.runtime is None:
                self._release_profile_claim(state)
            raise
        state.runtime = runtime
        state.runtime_identity = runtime_identity
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
                purpose=purpose,
                exclusive=exclusive,
                generation=state.generation,
                profile_dir=self.resolve_profile_dir(account_id),
                runtime=runtime,
            )

    async def release_runtime(
        self,
        lease: Optional[AccountBrowserRuntimeLease],
        *,
        reason: str = "released",
    ) -> None:
        _ = reason
        if lease is None or lease.released:
            return
        state = self._states.get(lease.account_id)
        if state is None:
            lease.released = True
            return
        pages_to_close = list(lease.pages)
        lease.pages.clear()
        for page in pages_to_close:
            try:
                await _close_async_page(page)
            except Exception:
                pass
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
            lease.released = True
            state.condition.notify_all()
        for runtime, close_reason in closures_to_run:
            await self._close_async_runtime(runtime, reason=close_reason)
        if should_release_claim:
            self._release_profile_claim(state)

    async def get_fresh_page(self, lease: AccountBrowserRuntimeLease) -> Tuple[Any, Any]:
        if lease.released:
            raise RuntimeError("runtime lease has already been released")
        context = getattr(lease.runtime, "context", None)
        if context is None:
            raise RuntimeError("runtime context is unavailable")
        new_page = getattr(context, "new_page", None)
        if not callable(new_page):
            raise RuntimeError("runtime context cannot create pages")
        page = new_page()
        if inspect.isawaitable(page):
            page = await page
        lease.pages.append(page)
        return page, context

    async def invalidate_runtime(self, account_id: str, *, reason: str = "invalidated") -> bool:
        account_id = self._normalize_account_id(account_id)
        state = self._states.setdefault(account_id, _RuntimeState())
        should_release_claim = False
        async with state.condition:
            runtime = state.runtime
            if runtime is None:
                return False
            state.runtime = None
            state.runtime_identity = None
            state.generation += 1
            if state.active_leases > 0:
                state.pending_closures.append((runtime, reason))
                return True
            should_release_claim = True
        await self._close_async_runtime(runtime, reason=reason)
        if should_release_claim:
            self._release_profile_claim(state)
        return True

    async def cleanup_idle_runtimes(self) -> int:
        closed_count = 0
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
            await self._close_async_runtime(runtime_to_close, reason="idle_timeout")
            if should_release_claim:
                self._release_profile_claim(state)
            closed_count += 1
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
        )
        runtime_identity = _build_runtime_identity(
            profile_dir,
            runtime_request,
            default_persistent_context=False,
        )
        if _runtime_is_alive(state.runtime) and state.owner_thread_id == current_thread_id:
            if state.runtime_identity is not None and state.runtime_identity != runtime_identity:
                raise ValueError("同账号已存在不兼容的 sync runtime，请先失效再重建")
            return state.runtime
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
            state.owner_thread_id = None
            state.generation += 1
            closures_to_run = self._defer_or_close_sync_runtime(
                state,
                stale_runtime,
                reason=stale_reason,
                owner_thread_id=stale_owner_thread_id,
                current_thread_id=current_thread_id,
            )
            for runtime, close_reason in closures_to_run:
                self._close_sync_runtime(runtime, reason=close_reason)
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
        with self._sync_states_guard:
            state = self._sync_states.setdefault(account_id, _SyncRuntimeState())
        current_thread_id = threading.get_ident()
        closures_to_run = []
        with state.condition:
            self._raise_if_runtime_draining_pending_closures(
                state,
                account_id=account_id,
                mode="sync",
            )
            while state.active_leases and (exclusive or state.active_exclusive):
                state.condition.wait()
                self._raise_if_runtime_draining_pending_closures(
                    state,
                    account_id=account_id,
                    mode="sync",
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
                purpose=purpose,
                exclusive=exclusive,
                generation=state.generation,
                profile_dir=self.resolve_profile_dir(account_id),
                runtime=runtime,
            )
        for runtime_to_close, close_reason in closures_to_run:
            self._close_sync_runtime(runtime_to_close, reason=close_reason)
        return lease

    def release_runtime_sync(
        self,
        lease: Optional[SyncAccountBrowserRuntimeLease],
        *,
        reason: str = "released",
    ) -> None:
        _ = reason
        if lease is None or lease.released:
            return
        state = self._sync_states.get(lease.account_id)
        if state is None:
            lease.released = True
            return
        pages_to_close = list(lease.pages)
        lease.pages.clear()
        for page in pages_to_close:
            try:
                _close_sync_page(page)
            except Exception:
                pass
        current_thread_id = threading.get_ident()
        closures_to_run = []
        should_release_claim = False
        with state.condition:
            if state.active_leases > 0:
                state.active_leases -= 1
            if state.active_leases == 0:
                state.active_exclusive = False
                state.last_released_at = self.time_fn()
                closures_to_run = self._take_sync_closures_for_thread(state, current_thread_id)
                should_release_claim = state.runtime is None
            lease.released = True
            state.condition.notify_all()
        for runtime_to_close, close_reason in closures_to_run:
            self._close_sync_runtime(runtime_to_close, reason=close_reason)
        if should_release_claim:
            self._release_profile_claim(state)

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
        page = context.new_page()
        runtime.page = page
        lease.pages.append(page)
        return page, context

    def invalidate_runtime_sync(self, account_id: str, *, reason: str = "invalidated") -> bool:
        account_id = self._normalize_account_id(account_id)
        with self._sync_states_guard:
            state = self._sync_states.setdefault(account_id, _SyncRuntimeState())
        current_thread_id = threading.get_ident()
        closures_to_run = []
        should_release_claim = False
        with state.condition:
            runtime = state.runtime
            owner_thread_id = state.owner_thread_id
            if runtime is None:
                return False
            state.runtime = None
            state.runtime_identity = None
            state.owner_thread_id = None
            state.generation += 1
            if state.active_leases > 0:
                state.pending_closures.append((runtime, reason, owner_thread_id))
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
        for runtime_to_close, close_reason in closures_to_run:
            self._close_sync_runtime(runtime_to_close, reason=close_reason)
        if should_release_claim:
            self._release_profile_claim(state)
        return True

    def cleanup_idle_runtimes_sync(self) -> int:
        closed_count = 0
        current_thread_id = threading.get_ident()
        with self._sync_states_guard:
            states = list(self._sync_states.items())
        for _account_id, state in states:
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
                    state.runtime = None
                    state.runtime_identity = None
                    state.owner_thread_id = None
                    state.generation += 1
                    closures_to_run.extend(
                        self._defer_or_close_sync_runtime(
                            state,
                            runtime_to_close,
                            reason="idle_timeout",
                            owner_thread_id=owner_thread_id,
                            current_thread_id=current_thread_id,
                        )
                    )
                    should_release_claim = True
                closures_to_run.extend(self._take_sync_closures_for_thread(state, current_thread_id))
            if not closures_to_run:
                continue
            for runtime_to_close, close_reason in closures_to_run:
                self._close_sync_runtime(runtime_to_close, reason=close_reason)
                closed_count += 1
            if should_release_claim:
                self._release_profile_claim(state)
        return closed_count


account_browser_runtime_manager = AccountBrowserRuntimeManager()
