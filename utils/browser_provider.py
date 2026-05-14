from __future__ import annotations

import asyncio
import inspect
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence
from urllib.parse import quote, urlparse, urlunparse

from cloakbrowser import (
    build_args,
    ensure_binary,
    launch as cloak_launch,
    launch_async as cloak_launch_async,
    launch_context as cloak_launch_context,
    launch_context_async as cloak_launch_context_async,
    launch_persistent_context as cloak_launch_persistent_context,
    launch_persistent_context_async as cloak_launch_persistent_context_async,
    maybe_resolve_geoip,
)

BrowserLike = Any
BrowserContextLike = Any
PageLike = Any

DEFAULT_CDP_HOST = "127.0.0.1"
DEFAULT_STARTUP_TIMEOUT = 10.0
DEFAULT_CLOSE_TIMEOUT = 5.0
DEVTOOLS_ACTIVE_PORT = "DevToolsActivePort"


@dataclass
class ManagedBrowserRuntime:
    process: Any
    browser: Optional[BrowserLike] = None
    playwright: Any = None
    user_data_dir: Optional[str] = None
    cdp_endpoint: Optional[str] = None
    close_reason: Optional[str] = None

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None


@dataclass
class AsyncManagedBrowserRuntime:
    process: Any
    browser: Optional[BrowserLike] = None
    playwright: Any = None
    user_data_dir: Optional[str] = None
    cdp_endpoint: Optional[str] = None
    close_reason: Optional[str] = None

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None


def build_download_proxy_env(
    proxy_url: Optional[str],
    base_env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    env = dict(base_env or {})
    if proxy_url:
        env["HTTP_PROXY"] = proxy_url
        env["HTTPS_PROXY"] = proxy_url
        env["http_proxy"] = proxy_url
        env["https_proxy"] = proxy_url
    return env


def launch_browser(**kwargs):
    return cloak_launch(**kwargs)


async def launch_browser_async(**kwargs):
    return await cloak_launch_async(**kwargs)


def launch_browser_context(**kwargs):
    return cloak_launch_context(**kwargs)


async def launch_browser_context_async(**kwargs):
    return await cloak_launch_context_async(**kwargs)


def launch_browser_persistent_context(**kwargs):
    return cloak_launch_persistent_context(**kwargs)


async def launch_browser_persistent_context_async(**kwargs):
    return await cloak_launch_persistent_context_async(**kwargs)


def _ensure_proxy_scheme(proxy_url: str) -> str:
    proxy_url = str(proxy_url or "").strip()
    if not proxy_url:
        return ""
    if "://" in proxy_url:
        return proxy_url
    return f"http://{proxy_url}"


def _build_proxy_server_url(proxy: Any) -> tuple[Optional[str], Optional[str]]:
    if proxy is None:
        return None, None

    if isinstance(proxy, dict):
        server = _ensure_proxy_scheme(proxy.get("server"))
        if not server:
            return None, None
        bypass = str(proxy.get("bypass") or "").strip() or None
        username = str(proxy.get("username") or "")
        password = str(proxy.get("password") or "")
        if username or password:
            parsed = urlparse(server)
            hostname = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
            userinfo = quote(username, safe="")
            if password or username:
                userinfo = f"{userinfo}:{quote(password, safe='')}"
            netloc = f"{userinfo}@{hostname}{port}" if userinfo else f"{hostname}{port}"
            server = urlunparse(
                (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
            )
        return server, bypass

    server = _ensure_proxy_scheme(proxy)
    if not server:
        return None, None
    return server, None


def _build_managed_browser_launch_args(
    *,
    args: Optional[Sequence[str]],
    headless: bool,
    proxy: Any = None,
    stealth_args: bool = True,
    timezone: Optional[str] = None,
    locale: Optional[str] = None,
    geoip: bool = False,
) -> list[str]:
    resolved_timezone, resolved_locale, exit_ip = maybe_resolve_geoip(
        geoip,
        proxy,
        timezone,
        locale,
    )
    resolved_args = list(args or [])
    # NOTE:
    # Our "managed runtime" launches Chromium via subprocess (not Playwright),
    # so `headless=True` must be expressed as Chromium CLI args, otherwise it
    # will still open a visible window on Windows.
    if headless and not any(arg == "--headless" or arg.startswith("--headless=") for arg in resolved_args):
        # Prefer the "new" headless implementation when supported.
        resolved_args.append("--headless=new")
    proxy_server, proxy_bypass = _build_proxy_server_url(proxy)
    if proxy_server and not any(arg.startswith("--proxy-server=") for arg in resolved_args):
        resolved_args.append(f"--proxy-server={proxy_server}")
    if proxy_bypass and not any(arg.startswith("--proxy-bypass-list=") for arg in resolved_args):
        resolved_args.append(f"--proxy-bypass-list={proxy_bypass}")
    if exit_ip and not any(arg.startswith("--fingerprint-webrtc-ip=") for arg in resolved_args):
        resolved_args.append(f"--fingerprint-webrtc-ip={exit_ip}")
    return build_args(
        stealth_args,
        resolved_args,
        timezone=resolved_timezone,
        locale=resolved_locale,
        headless=headless,
    )


def _patch_browser_humanize_sync(
    browser: Any,
    *,
    humanize: bool,
    human_preset: str,
    human_config: Optional[Dict[str, Any]],
) -> None:
    if not humanize or browser is None:
        return
    from cloakbrowser.human import patch_browser
    from cloakbrowser.human.config import resolve_config

    patch_browser(browser, resolve_config(human_preset, human_config))


async def _patch_browser_humanize_async(
    browser: Any,
    *,
    humanize: bool,
    human_preset: str,
    human_config: Optional[Dict[str, Any]],
) -> None:
    if not humanize or browser is None:
        return
    from cloakbrowser.human import patch_browser_async
    from cloakbrowser.human.config import resolve_config

    await patch_browser_async(browser, resolve_config(human_preset, human_config))


def _ensure_managed_launch_args(
    args: Optional[Sequence[str]],
    user_data_dir: str,
    cdp_host: str,
) -> list[str]:
    managed_args = list(args or [])
    if not any(arg.startswith("--user-data-dir=") for arg in managed_args):
        managed_args.append(f"--user-data-dir={user_data_dir}")
    if not any(arg.startswith("--remote-debugging-port=") for arg in managed_args):
        managed_args.append("--remote-debugging-port=0")
    if not any(arg.startswith("--remote-debugging-address=") for arg in managed_args):
        managed_args.append(f"--remote-debugging-address={cdp_host}")
    return managed_args


def _read_devtools_active_port(
    user_data_dir: str,
    startup_timeout: float,
    process: Any,
    sleep: Callable[[float], None],
) -> int:
    port_file = Path(user_data_dir) / DEVTOOLS_ACTIVE_PORT
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("CloakBrowser process exited before DevToolsActivePort was ready")
        if port_file.exists():
            lines = port_file.read_text(encoding="utf-8").splitlines()
            if lines and lines[0].strip():
                return int(lines[0].strip())
        sleep(0.1)
    raise TimeoutError(f"Timed out waiting for {DEVTOOLS_ACTIVE_PORT}")


async def _read_devtools_active_port_async(
    user_data_dir: str,
    startup_timeout: float,
    process: Any,
) -> int:
    port_file = Path(user_data_dir) / DEVTOOLS_ACTIVE_PORT
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("CloakBrowser process exited before DevToolsActivePort was ready")
        if port_file.exists():
            lines = port_file.read_text(encoding="utf-8").splitlines()
            if lines and lines[0].strip():
                return int(lines[0].strip())
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for {DEVTOOLS_ACTIVE_PORT}")


def _connect_over_cdp_sync(endpoint: str) -> tuple[Any, BrowserLike]:
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = playwright.chromium.connect_over_cdp(endpoint)
    return playwright, browser


async def _connect_over_cdp_async(endpoint: str) -> tuple[Any, BrowserLike]:
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    browser = await playwright.chromium.connect_over_cdp(endpoint)
    return playwright, browser


async def _wait_for_process_exit_async(process: Any, close_timeout: float) -> None:
    wait = getattr(process, "wait", None)
    if not callable(wait):
        return

    if inspect.iscoroutinefunction(wait):
        await asyncio.wait_for(wait(), timeout=close_timeout)
        return

    try:
        await asyncio.to_thread(wait, timeout=close_timeout)
    except TypeError:
        await asyncio.to_thread(wait)


def launch_managed_browser_runtime(
    user_data_dir: str,
    *,
    executable_path: Optional[str] = None,
    args: Optional[Sequence[str]] = None,
    headless: bool = True,
    proxy: Any = None,
    stealth_args: bool = True,
    timezone: Optional[str] = None,
    locale: Optional[str] = None,
    geoip: bool = False,
    humanize: bool = False,
    human_preset: str = "default",
    human_config: Optional[Dict[str, Any]] = None,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    cdp_host: str = DEFAULT_CDP_HOST,
    _process_factory: Callable[..., Any] = subprocess.Popen,
    _connect_over_cdp: Optional[Callable[[str], Any]] = None,
    _port_reader: Optional[Callable[[str, float, Any, Callable[[float], None]], int]] = None,
    _sleep: Callable[[float], None] = time.sleep,
) -> ManagedBrowserRuntime:
    # Best-effort cleanup of stale profile lock artifacts.
    #
    # - Chrome will write DevToolsActivePort into the profile dir. If the previous
    #   run was killed abruptly, the file may remain and point to a stale port,
    #   causing connect_over_cdp() to ECONNREFUSED.
    # - Some environments may leave lock markers like "lockfile" or Chromium
    #   singleton files behind, which can make the next launch exit early.
    for stale_name in (
        DEVTOOLS_ACTIVE_PORT,
        "lockfile",
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
    ):
        try:
            (Path(user_data_dir) / stale_name).unlink()
        except FileNotFoundError:
            pass
        except Exception:
            # Best-effort cleanup; the launcher will retry or fail with a clearer error.
            pass

    resolved_executable_path = executable_path or ensure_binary()
    chrome_args = _build_managed_browser_launch_args(
        args=args,
        headless=headless,
        proxy=proxy,
        stealth_args=stealth_args,
        timezone=timezone,
        locale=locale,
        geoip=geoip,
    )
    command = [
        resolved_executable_path,
        *_ensure_managed_launch_args(chrome_args, user_data_dir, cdp_host),
    ]
    process = _process_factory(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    runtime = ManagedBrowserRuntime(process=process, user_data_dir=user_data_dir)
    port_reader = _port_reader or _read_devtools_active_port
    connector = _connect_over_cdp or _connect_over_cdp_sync

    try:
        port = port_reader(user_data_dir, startup_timeout, process, _sleep)
        runtime.cdp_endpoint = f"http://{cdp_host}:{port}"
        connected = connector(runtime.cdp_endpoint)
        if isinstance(connected, tuple):
            runtime.playwright, runtime.browser = connected
        else:
            runtime.browser = connected
        _patch_browser_humanize_sync(
            runtime.browser,
            humanize=humanize,
            human_preset=human_preset,
            human_config=human_config,
        )
        return runtime
    except Exception:
        close_managed_browser_runtime(runtime, reason="attach_failed")
        raise


async def launch_managed_browser_runtime_async(
    user_data_dir: str,
    *,
    executable_path: Optional[str] = None,
    args: Optional[Sequence[str]] = None,
    headless: bool = True,
    proxy: Any = None,
    stealth_args: bool = True,
    timezone: Optional[str] = None,
    locale: Optional[str] = None,
    geoip: bool = False,
    humanize: bool = False,
    human_preset: str = "default",
    human_config: Optional[Dict[str, Any]] = None,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    cdp_host: str = DEFAULT_CDP_HOST,
    _process_factory: Callable[..., Any] = subprocess.Popen,
    _connect_over_cdp: Optional[Callable[[str], Any]] = None,
    _port_reader: Optional[Callable[[str, float, Any], Any]] = None,
) -> AsyncManagedBrowserRuntime:
    # See sync variant above: clear stale profile lock artifacts before launching.
    for stale_name in (
        DEVTOOLS_ACTIVE_PORT,
        "lockfile",
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
    ):
        try:
            (Path(user_data_dir) / stale_name).unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass

    resolved_executable_path = executable_path or ensure_binary()
    chrome_args = _build_managed_browser_launch_args(
        args=args,
        headless=headless,
        proxy=proxy,
        stealth_args=stealth_args,
        timezone=timezone,
        locale=locale,
        geoip=geoip,
    )
    command = [
        resolved_executable_path,
        *_ensure_managed_launch_args(chrome_args, user_data_dir, cdp_host),
    ]
    process = _process_factory(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    runtime = AsyncManagedBrowserRuntime(process=process, user_data_dir=user_data_dir)
    port_reader = _port_reader or _read_devtools_active_port_async
    connector = _connect_over_cdp or _connect_over_cdp_async

    try:
        port = await port_reader(user_data_dir, startup_timeout, process)
        runtime.cdp_endpoint = f"http://{cdp_host}:{port}"
        connected = await connector(runtime.cdp_endpoint)
        if isinstance(connected, tuple):
            runtime.playwright, runtime.browser = connected
        else:
            runtime.browser = connected
        await _patch_browser_humanize_async(
            runtime.browser,
            humanize=humanize,
            human_preset=human_preset,
            human_config=human_config,
        )
        return runtime
    except Exception:
        await close_managed_browser_runtime_async(runtime, reason="attach_failed")
        raise


def close_managed_browser_runtime(
    runtime: ManagedBrowserRuntime,
    *,
    reason: str = "closed",
    close_timeout: float = DEFAULT_CLOSE_TIMEOUT,
) -> ManagedBrowserRuntime:
    if runtime.close_reason is None:
        runtime.close_reason = reason

    if runtime.browser is not None:
        try:
            runtime.browser.close()
        except Exception:
            pass
        runtime.browser = None

    if runtime.playwright is not None:
        try:
            runtime.playwright.stop()
        except Exception:
            pass
        runtime.playwright = None

    process = runtime.process
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=close_timeout)
        except Exception:
            process.kill()
            try:
                process.wait(timeout=close_timeout)
            except Exception:
                pass

    return runtime


async def close_managed_browser_runtime_async(
    runtime: AsyncManagedBrowserRuntime,
    *,
    reason: str = "closed",
    close_timeout: float = DEFAULT_CLOSE_TIMEOUT,
) -> AsyncManagedBrowserRuntime:
    if runtime.close_reason is None:
        runtime.close_reason = reason

    if runtime.browser is not None:
        try:
            # Playwright's Browser.close() may hang (especially for CDP-attached
            # runtimes). Always bound it with a timeout so runtime invalidation
            # cannot freeze the service thread forever.
            await asyncio.wait_for(runtime.browser.close(), timeout=close_timeout)
        except Exception:
            pass
        runtime.browser = None

    if runtime.playwright is not None:
        try:
            await asyncio.wait_for(runtime.playwright.stop(), timeout=close_timeout)
        except Exception:
            pass
        runtime.playwright = None

    process = runtime.process
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            await _wait_for_process_exit_async(process, close_timeout)
        except Exception:
            process.kill()
            try:
                await _wait_for_process_exit_async(process, close_timeout)
            except Exception:
                pass

    return runtime


def close_managed_runtime_handle(
    runtime: ManagedBrowserRuntime,
    *,
    reason: str = "closed",
    close_timeout: float = DEFAULT_CLOSE_TIMEOUT,
) -> ManagedBrowserRuntime:
    return close_managed_browser_runtime(
        runtime,
        reason=reason,
        close_timeout=close_timeout,
    )


async def close_managed_runtime_handle_async(
    runtime: ManagedBrowserRuntime | AsyncManagedBrowserRuntime,
    *,
    reason: str = "closed",
    close_timeout: float = DEFAULT_CLOSE_TIMEOUT,
) -> ManagedBrowserRuntime | AsyncManagedBrowserRuntime:
    if isinstance(runtime, AsyncManagedBrowserRuntime):
        return await close_managed_browser_runtime_async(
            runtime,
            reason=reason,
            close_timeout=close_timeout,
        )
    return close_managed_browser_runtime(
        runtime,
        reason=reason,
        close_timeout=close_timeout,
    )
