from __future__ import annotations

import asyncio
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from cloakbrowser import (
    launch as cloak_launch,
    launch_async as cloak_launch_async,
    launch_context as cloak_launch_context,
    launch_context_async as cloak_launch_context_async,
    launch_persistent_context as cloak_launch_persistent_context,
    launch_persistent_context_async as cloak_launch_persistent_context_async,
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


def launch_managed_browser_runtime(
    executable_path: str,
    user_data_dir: str,
    *,
    args: Optional[Sequence[str]] = None,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    cdp_host: str = DEFAULT_CDP_HOST,
    _process_factory: Callable[..., Any] = subprocess.Popen,
    _connect_over_cdp: Optional[Callable[[str], Any]] = None,
    _port_reader: Optional[Callable[[str, float, Any, Callable[[float], None]], int]] = None,
    _sleep: Callable[[float], None] = time.sleep,
) -> ManagedBrowserRuntime:
    command = [executable_path, *_ensure_managed_launch_args(args, user_data_dir, cdp_host)]
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
        return runtime
    except Exception:
        close_managed_browser_runtime(runtime, reason="attach_failed")
        raise


async def launch_managed_browser_runtime_async(
    executable_path: str,
    user_data_dir: str,
    *,
    args: Optional[Sequence[str]] = None,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    cdp_host: str = DEFAULT_CDP_HOST,
    _process_factory: Callable[..., Any] = subprocess.Popen,
    _connect_over_cdp: Optional[Callable[[str], Any]] = None,
    _port_reader: Optional[Callable[[str, float, Any], Any]] = None,
) -> AsyncManagedBrowserRuntime:
    command = [executable_path, *_ensure_managed_launch_args(args, user_data_dir, cdp_host)]
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
            await runtime.browser.close()
        except Exception:
            pass
        runtime.browser = None

    if runtime.playwright is not None:
        try:
            await runtime.playwright.stop()
        except Exception:
            pass
        runtime.playwright = None

    process = runtime.process
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=close_timeout)
        except Exception:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=close_timeout)
            except Exception:
                pass

    return runtime
