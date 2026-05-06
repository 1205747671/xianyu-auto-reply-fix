# CloakBrowser Provider Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把仓库里所有活跃浏览器链路统一切到 `CloakBrowser`，删除旧的 `playwright` / `patchright` 直连与后端切换逻辑，并保持现有账号级 profile 方案可继续工作。

**Architecture:** 先新增一个 `utils/browser_provider.py` 作为唯一浏览器 provider 适配层，统一封装 `launch`、`launch_async`、`launch_context`、`launch_context_async`、`launch_persistent_context`、`launch_persistent_context_async` 这些官方能力。再把 `utils/xianyu_slider_stealth.py`、`XianyuAutoAsync.py`、`reply_server.py`、调试脚本和旁路模块逐个切到这个适配层，最后收口启动、Docker、依赖和文档。因为仓库里还残留 DrissionPage 入口，而用户要求“所有浏览器操作都替换”，本计划同时把公开可调用的 DrissionPage 路径切掉或删除。

**Tech Stack:** Python, `unittest`, `cloakbrowser`, `apply_patch`, Docker Compose

---

## 文件边界

- Create: `utils/browser_provider.py`
  - 统一封装 CloakBrowser sync / async 启动、persistent context 启动、下载代理环境构造、类型别名。
- Create: `tests/test_browser_provider.py`
  - 只测 provider 适配层，不把业务逻辑和 provider 细节搅成一锅粥。
- Modify: `utils/xianyu_slider_stealth.py`
  - 主登录 / 滑块入口，去掉 `playwright` / `patchright` / DrissionPage 直连。
- Modify: `tests/test_slider_verification_guards.py`
  - 把 fake runtime 从 fake Playwright 收口成 fake provider runtime，守住登录与 persistent profile 分支。
- Modify: `XianyuAutoAsync.py`
  - 去掉 `_start_playwright_safe` 和 `playwright.stop()` 生命周期，统一走 provider async helpers。
- Modify: `tests/test_xianyu_token_refresh_request.py`
  - 守住 `token_refresh` 风控接管链仍然正确给 `XianyuSliderStealth` 传参。
- Create: `tests/test_xianyu_async_browser_runtime.py`
  - 守住 `XianyuAutoAsync.py` 的 async 浏览器启动和关闭不再依赖 `playwright.stop()`。
- Create: `tests/test_browser_sidecars.py`
  - 守住搜索、订单、二维码验证等旁路浏览器链也已经切到 provider 适配层。
- Modify: `utils/item_search.py`
- Modify: `utils/order_detail_fetcher.py`
- Modify: `utils/qr_login.py`
- Modify: `utils/captcha_remote_control.py`
  - 全部去掉对 `playwright.async_api` 的直接依赖。
- Modify: `reply_server.py`
- Modify: `debug_manual_password_login.py`
- Modify: `debug_manual_cookie_slider.py`
  - 改方法名、删 `--automation-backend`、删环境变量分支。
- Create: `tests/test_start_browser_runtime.py`
- Modify: `Start.py`
  - 把安装与自检逻辑从 Playwright 改成 CloakBrowser。
- Modify: `requirements.txt`
- Modify: `.gitignore`
- Modify: `.dockerignore`
- Modify: `Dockerfile`
- Modify: `Dockerfile-cn`
- Modify: `docker-compose.yml`
- Modify: `docker-compose-cn.yml`
- Modify: `README.md`
- Modify: `PASSWORD_LOGIN_VERIFICATION_FLOW_FIX_SUMMARY_20260504.md`
  - 依赖、构建、运行和文档全部收口到 CloakBrowser。
- Delete: `utils/slider_patch.py`
  - 已无活跃引用，保留只会污染认知。
- Delete: `utils/refresh_util.py`
  - 当前无引用且仍依赖 DrissionPage，继续留着只会让迁移验收发臭。

### Task 1: 先搭 provider 适配层，再把 CloakBrowser 官方 API 收口

**Files:**
- Create: `tests/test_browser_provider.py`
- Create: `utils/browser_provider.py`

- [ ] **Step 1: 写失败测试，钉住 provider 适配层的最小契约**

```python
import unittest
from unittest import IsolatedAsyncioTestCase, mock


class BrowserProviderTest(unittest.TestCase):
    def test_build_download_proxy_env_sets_http_and_https(self):
        from utils.browser_provider import build_download_proxy_env

        env = build_download_proxy_env("http://127.0.0.1:1081", {"PATH": "x"})

        self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:1081")
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:1081")
        self.assertEqual(env["PATH"], "x")

    @mock.patch("utils.browser_provider.cloak_launch")
    def test_launch_browser_delegates_to_cloakbrowser_launch(self, mock_launch):
        from utils.browser_provider import launch_browser

        launch_browser(headless=True, args=["--foo"])

        mock_launch.assert_called_once_with(headless=True, args=["--foo"])


class BrowserProviderAsyncTest(IsolatedAsyncioTestCase):
    @mock.patch("utils.browser_provider.cloak_launch_persistent_context_async")
    async def test_launch_browser_persistent_context_async_delegates(self, mock_launch):
        from utils.browser_provider import launch_browser_persistent_context_async

        await launch_browser_persistent_context_async(
            user_data_dir="browser_data/user_1",
            headless=True,
            args=["--bar"],
        )

        mock_launch.assert_awaited_once_with(
            user_data_dir="browser_data/user_1",
            headless=True,
            args=["--bar"],
        )
```

- [ ] **Step 2: 跑测试，确认当前仓库还没有这个适配层**

Run: `python -m unittest tests.test_browser_provider -v`

Expected: `No module named 'utils.browser_provider'`

- [ ] **Step 3: 写最小实现，直接封装 CloakBrowser 官方 API**

```python
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


def build_download_proxy_env(proxy_url: Optional[str], base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
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
```

- [ ] **Step 4: 重跑测试，确认适配层已经成立**

Run: `python -m unittest tests.test_browser_provider -v`

Expected: `OK`

- [ ] **Step 5: 提交第一刀，别把 provider 适配层和业务改动搅一起**

```bash
git add tests/test_browser_provider.py utils/browser_provider.py
git commit -m "feat: add cloakbrowser provider adapter"
```

### Task 2: 切主登录 / 滑块链，删掉 patchright、旧 Playwright 命名和公开 DrissionPage 入口

**Files:**
- Modify: `tests/test_slider_verification_guards.py`
- Modify: `utils/xianyu_slider_stealth.py`
- Modify: `reply_server.py`
- Modify: `debug_manual_password_login.py`
- Modify: `debug_manual_cookie_slider.py`

- [ ] **Step 1: 先写失败测试，钉住 persistent context 和新方法名**

```python
    @mock.patch("utils.xianyu_slider_stealth.launch_browser_persistent_context")
    def test_init_browser_uses_provider_persistent_context_when_profile_enabled(self, mock_launch):
        slider = XianyuSliderStealth.__new__(XianyuSliderStealth)
        slider.use_account_persistent_profile = True
        slider.account_persistent_profile_dir = "browser_data/user_1"
        slider.headless = True
        slider.browser = None
        slider.context = None
        slider.page = None
        slider._build_browser_proxy_settings = lambda: None
        slider._build_browser_context_options = lambda _features: {}
        slider._build_browser_features = lambda: {}
        slider._build_browser_launch_args = lambda: ["--foo"]

        fake_context = mock.Mock()
        fake_context.pages = [mock.Mock()]
        mock_launch.return_value = fake_context

        slider.init_browser()

        mock_launch.assert_called_once()

    def test_login_with_password_headful_is_alias_of_new_browser_login(self):
        slider = XianyuSliderStealth.__new__(XianyuSliderStealth)
        slider.login_with_password_browser = mock.Mock(return_value={"cookie2": "ok"})

        result = slider.login_with_password_headful("user", "pass", show_browser=True)

        self.assertEqual(result, {"cookie2": "ok"})
        slider.login_with_password_browser.assert_called_once_with("user", "pass", show_browser=True)
```

- [ ] **Step 2: 跑测试，确认当前还是旧链路**

Run: `python -m unittest tests.test_slider_verification_guards -v`

Expected: FAIL，提示 `launch_browser_persistent_context` 不存在，或 `login_with_password_headful` 仍然走旧 DrissionPage 实现

- [ ] **Step 3: 最小实现主登录改造**

```python
from utils.browser_provider import (
    launch_browser,
    launch_browser_persistent_context,
)


def _build_browser_proxy_settings(self) -> Optional[Dict[str, str]]:
    proxy_url = self._resolve_effective_proxy_url()
    if not proxy_url:
        return None
    return {"server": proxy_url}


def init_browser(self):
    if self.use_account_persistent_profile:
        self.context = launch_browser_persistent_context(
            user_data_dir=self.account_persistent_profile_dir,
            headless=self.headless,
            proxy=self._build_browser_proxy_settings(),
            args=self._build_browser_launch_args(),
        )
        self.browser = self.context.browser
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return

    self.browser = launch_browser(
        headless=self.headless,
        proxy=self._build_browser_proxy_settings(),
        args=self._build_browser_launch_args(),
    )
    self.context = self.browser.new_context(**self._build_browser_context_options(self._build_browser_features()))
    self.page = self.context.new_page()


def login_with_password_browser(self, account: str, password: str, show_browser: bool = False, **kwargs):
    return self._login_with_browser_flow(account, password, show_browser=show_browser, **kwargs)


def login_with_password_headful(self, account: str = None, password: str = None, show_browser: bool = False):
    return self.login_with_password_browser(account, password, show_browser=True)
```

- [ ] **Step 4: 切调用方，去掉 `--automation-backend` 和旧方法名**

```python
# debug_manual_password_login.py
parser.add_argument("--show-browser", action="store_true")
result = slider.login_with_password_browser(
    args.account,
    args.password,
    show_browser=args.show_browser,
)

# debug_manual_cookie_slider.py
print("browser_provider=cloakbrowser")

# reply_server.py
cookies_dict = slider_instance.login_with_password_browser(
    account=account,
    password=password,
    show_browser=show_browser,
)
```

- [ ] **Step 5: 重跑测试，确认主链不再碰旧 provider 分支**

Run: `python -m unittest tests.test_slider_verification_guards -v`

Expected: `OK`

- [ ] **Step 6: 提交主链切换**

```bash
git add tests/test_slider_verification_guards.py utils/xianyu_slider_stealth.py reply_server.py debug_manual_password_login.py debug_manual_cookie_slider.py
git commit -m "refactor: switch slider login flow to cloakbrowser"
```

### Task 3: 把 `XianyuAutoAsync` 的 async 浏览器生命周期改成 provider-neutral

**Files:**
- Create: `tests/test_xianyu_async_browser_runtime.py`
- Modify: `XianyuAutoAsync.py`

- [ ] **Step 1: 先写失败测试，钉住 async 启动和关闭不再依赖 `playwright.stop()`**

```python
import unittest
from unittest import mock

import XianyuAutoAsync


class AsyncBrowserRuntimeTest(unittest.IsolatedAsyncioTestCase):
    @mock.patch("XianyuAutoAsync.launch_browser_async", new_callable=mock.AsyncMock)
    async def test_launch_browser_safe_delegates_to_provider(self, mock_launch):
        fake_browser = object()
        mock_launch.return_value = fake_browser

        browser = await XianyuAutoAsync._launch_browser_safe(
            "unit_cookie",
            headless=True,
            args=["--foo"],
        )

        self.assertIs(browser, fake_browser)
        mock_launch.assert_awaited_once_with(headless=True, args=["--foo"])

    async def test_normal_close_resources_prefers_context_close(self):
        live = XianyuAutoAsync.XianyuLive.__new__(XianyuAutoAsync.XianyuLive)
        fake_browser = mock.AsyncMock()
        fake_context = mock.AsyncMock()

        await live._normal_close_resources(fake_browser, fake_context)

        fake_context.close.assert_awaited_once()
        fake_browser.close.assert_not_awaited()
```

- [ ] **Step 2: 跑测试，确认当前代码名还绑在 Playwright 上**

Run: `python -m unittest tests.test_xianyu_async_browser_runtime -v`

Expected: FAIL，提示 `_launch_browser_safe` 不存在，或 `_normal_close_resources(...)` 还依赖旧 `playwright.stop()` 生命周期

- [ ] **Step 3: 改掉 async 启动与关闭模型**

```python
from utils.browser_provider import launch_browser_async


async def _launch_browser_safe(cookie_id: str = "default", **kwargs):
    is_docker = _is_docker_env()
    old_policy = None
    if is_docker:
        old_policy = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(_DockerEventLoopPolicy())
    try:
        return await asyncio.wait_for(
            launch_browser_async(**kwargs),
            timeout=30.0,
        )
    finally:
        if old_policy:
            asyncio.set_event_loop_policy(old_policy)


async def _normal_close_resources(self, browser, context=None):
    if context:
        await asyncio.wait_for(context.close(), timeout=5.0)
    elif browser:
        await asyncio.wait_for(browser.close(), timeout=5.0)
```

- [ ] **Step 4: 把所有 `async_playwright().start()` / `playwright.stop()` 调用点替成 provider helper**

```python
browser = await _launch_browser_safe(
    self.cookie_id,
    headless=self.headless,
    proxy=self._build_browser_proxy_settings(),
    args=browser_args,
)

context = await browser.new_context(**context_options)
page = await context.new_page()
```

- [ ] **Step 5: 重跑 async 相关测试**

Run: `python -m unittest tests.test_xianyu_async_browser_runtime tests.test_xianyu_token_refresh_request -v`

Expected: `OK`

- [ ] **Step 6: 提交 async 生命周期改造**

```bash
git add tests/test_xianyu_async_browser_runtime.py XianyuAutoAsync.py
git commit -m "refactor: remove playwright runtime lifecycle"
```

### Task 4: 把搜索 / 订单 / 二维码验证这些旁路浏览器链全部切到 provider

**Files:**
- Create: `tests/test_browser_sidecars.py`
- Modify: `utils/item_search.py`
- Modify: `utils/order_detail_fetcher.py`
- Modify: `utils/qr_login.py`
- Modify: `utils/captcha_remote_control.py`

- [ ] **Step 1: 先写失败测试，钉住旁路模块不再直连 `playwright.async_api`**

```python
import unittest
from unittest import IsolatedAsyncioTestCase, mock


class BrowserSidecarAsyncTest(IsolatedAsyncioTestCase):
    @mock.patch("utils.item_search.launch_browser_persistent_context_async")
    async def test_item_search_uses_provider_persistent_context(self, mock_launch):
        from utils.item_search import XianyuSearcher

        searcher = XianyuSearcher()
        fake_context = mock.AsyncMock()
        fake_context.browser = mock.Mock()
        fake_context.pages = []
        fake_context.new_page.return_value = mock.AsyncMock()
        mock_launch.return_value = fake_context

        await searcher.init_browser()

        mock_launch.assert_awaited_once()

    @mock.patch("utils.order_detail_fetcher.launch_browser_async")
    async def test_order_detail_fetcher_uses_provider_launch(self, mock_launch):
        from utils.order_detail_fetcher import OrderDetailFetcher

        fetcher = OrderDetailFetcher("cookie2=value")
        fake_browser = mock.AsyncMock()
        fake_browser.new_context.return_value = mock.AsyncMock()
        mock_launch.return_value = fake_browser

        await fetcher.init_browser()

        mock_launch.assert_awaited_once()
```

- [ ] **Step 2: 跑测试，确认旁路模块还没接 provider**

Run: `python -m unittest tests.test_browser_sidecars -v`

Expected: FAIL，提示模块里还没有 `launch_browser_async` / `launch_browser_persistent_context_async`

- [ ] **Step 3: 最小实现四个旁路模块切换**

```python
# utils/item_search.py
from utils.browser_provider import launch_browser_persistent_context_async

self.context = await launch_browser_persistent_context_async(
    user_data_dir=user_data_dir,
    headless=True,
    args=browser_args,
)

# utils/order_detail_fetcher.py
from utils.browser_provider import launch_browser_async

self.browser = await launch_browser_async(
    headless=headless,
    args=browser_args,
)

# utils/qr_login.py
from utils.browser_provider import launch_browser_async

browser = await launch_browser_async(
    headless=False,
    args=["--start-maximized"],
)

# utils/captcha_remote_control.py
from typing import Any

PageLike = Any


async def create_session(self, session_id: str, page: PageLike) -> Dict[str, str]:
    ...
```

- [ ] **Step 4: 再跑测试，确认旁路链已经一起切过去**

Run: `python -m unittest tests.test_browser_sidecars -v`

Expected: `OK`

- [ ] **Step 5: 提交旁路链改造**

```bash
git add tests/test_browser_sidecars.py utils/item_search.py utils/order_detail_fetcher.py utils/qr_login.py utils/captcha_remote_control.py
git commit -m "refactor: migrate browser sidecars to cloakbrowser"
```

### Task 5: 收口启动、依赖、Docker、文档和死代码清理

**Files:**
- Create: `tests/test_start_browser_runtime.py`
- Modify: `Start.py`
- Modify: `requirements.txt`
- Modify: `.gitignore`
- Modify: `.dockerignore`
- Modify: `Dockerfile`
- Modify: `Dockerfile-cn`
- Modify: `docker-compose.yml`
- Modify: `docker-compose-cn.yml`
- Modify: `README.md`
- Modify: `PASSWORD_LOGIN_VERIFICATION_FLOW_FIX_SUMMARY_20260504.md`
- Delete: `utils/slider_patch.py`
- Delete: `utils/refresh_util.py`

- [ ] **Step 1: 先写一个失败测试，钉住启动脚本不再提示 Playwright 安装命令**

```python
import unittest
from unittest import mock

import Start


class StartBrowserInstallMessageTest(unittest.TestCase):
    @mock.patch("builtins.print")
    @mock.patch("Start.importlib.import_module", side_effect=ImportError("missing"))
    def test_check_and_install_browser_runtime_mentions_cloakbrowser(self, _mock_import, mock_print):
        Start._check_and_install_browser_runtime()

        printed = "\n".join(" ".join(str(part) for part in call.args) for call in mock_print.call_args_list)
        self.assertIn("cloakbrowser", printed.lower())
        self.assertNotIn("playwright install chromium", printed.lower())
```

- [ ] **Step 2: 跑测试，确认启动脚本当前还是旧提示**

Run: `python -m unittest tests.test_start_browser_runtime -v`

Expected: FAIL，因为当前还是 `_check_and_install_playwright()` 和 `playwright install chromium`

- [ ] **Step 3: 改依赖与启动链**

```python
# Start.py
def _check_and_install_browser_runtime():
    try:
        import importlib
        importlib.import_module("cloakbrowser")
        return True
    except ImportError:
        print(f"{_ERROR} 未检测到 cloakbrowser，请先执行: pip install -r requirements.txt")
        print("   或手动运行: python -m cloakbrowser install")
        return False


if __name__ == "__main__":
    _check_and_install_browser_runtime()
```

```text
# requirements.txt
cloakbrowser
# 删除 playwright==1.59.0
# 删除 DrissionPage>=4.0.0
```

```dockerfile
# Dockerfile / Dockerfile-cn
RUN python -m cloakbrowser install
```

```yaml
# docker-compose-cn.yml
build:
  context: .
  dockerfile: Dockerfile-cn
  args:
    HTTP_PROXY: ${HTTP_PROXY}
    HTTPS_PROXY: ${HTTPS_PROXY}
    NO_PROXY: ${NO_PROXY}
    http_proxy: ${HTTP_PROXY}
    https_proxy: ${HTTPS_PROXY}
    no_proxy: ${NO_PROXY}
```

- [ ] **Step 4: 删掉死代码和旧文档表述**

```bash
git rm utils/slider_patch.py utils/refresh_util.py
```

```text
README.md:
- 把 “Playwright + DrissionPage + 浏览器自动化” 改成 “CloakBrowser + 浏览器自动化”
- 把 “playwright install chromium” 改成 “python -m cloakbrowser install”

PASSWORD_LOGIN_VERIFICATION_FLOW_FIX_SUMMARY_20260504.md:
- 明确 2026-05-06 起 provider 已切到 CloakBrowser
- 删除 patchright 默认后端、Playwright 安装命令、旧 fallback 说明

.gitignore / .dockerignore:
- 删除 `.playwright*`、`playwright-report*`、`DrissionPage*`、`.drission*` 这些旧 provider 缓存规则
- 如果后续实测确认 CloakBrowser 会生成固定缓存目录，再按新 provider 的真实目录补回 ignore 规则
```

- [ ] **Step 5: 做整体验证，防止漏网之鱼**

Run: `python -m unittest tests.test_browser_provider tests.test_slider_verification_guards tests.test_xianyu_token_refresh_request tests.test_xianyu_async_browser_runtime tests.test_browser_sidecars tests.test_start_browser_runtime -v`

Expected: `OK`

Run: `python -m py_compile Start.py XianyuAutoAsync.py utils\\browser_provider.py utils\\xianyu_slider_stealth.py utils\\item_search.py utils\\order_detail_fetcher.py utils\\qr_login.py utils\\captcha_remote_control.py`

Expected: no output

Run: `git grep -n "playwright\\|patchright\\|DrissionPage" -- .`

Expected: 仅迁移文档和仓库说明文件命中；业务代码、依赖文件、Docker 文件和 ignore 文件不再命中旧 provider

Run: `python release_precheck.py`

Expected: 发布前检查通过

- [ ] **Step 6: 提交收口改动**

```bash
git add Start.py requirements.txt .gitignore .dockerignore Dockerfile Dockerfile-cn docker-compose.yml docker-compose-cn.yml README.md PASSWORD_LOGIN_VERIFICATION_FLOW_FIX_SUMMARY_20260504.md
git commit -m "chore: finalize cloakbrowser migration"
```
