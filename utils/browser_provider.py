from __future__ import annotations

from typing import Any, Dict, Optional

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
