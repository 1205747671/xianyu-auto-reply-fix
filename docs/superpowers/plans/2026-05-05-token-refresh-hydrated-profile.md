# Token Refresh Hydrated Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `token_refresh` 命中的滑块恢复优先复用账号级 hydrated persistent profile，并保留旧链路 fallback。

**Architecture:** 调用侧仅在 `token_refresh` 场景显式打开 persistent profile 选项；`XianyuSliderStealth.init_browser()` 根据该选项优先走 `launch_persistent_context(...)`，失败时只在已知锁冲突场景回退旧的临时上下文流程。文档同步说明 `browser_data/user_<id>` 的角色与注意事项。

**Tech Stack:** Python, unittest, Playwright sync API, apply_patch

---

### Task 1: 先补失败测试，钉住新入口

**Files:**
- Modify: `tests/test_xianyu_token_refresh_request.py`

- [ ] **Step 1: 写失败测试，断言 `token_refresh` 创建滑块实例时显式打开账号 persistent profile**

- [ ] **Step 2: 运行单测确认当前代码还没传这个参数，测试先红**

Run: `pytest tests/test_xianyu_token_refresh_request.py -k persistent_profile -v`

Expected: FAIL，因当前 `_handle_captcha_verification(...)` 还没把 persistent profile 选项传给 `XianyuSliderStealth`

### Task 2: 再补失败测试，钉住浏览器初始化分支

**Files:**
- Modify: `tests/test_slider_verification_guards.py`

- [ ] **Step 1: 写失败测试，断言开启账号 persistent profile 时 `init_browser()` 优先走 `launch_persistent_context(...)`**

- [ ] **Step 2: 运行单测确认当前代码仍走 `launch(...) + new_context()`，测试先红**

Run: `pytest tests/test_slider_verification_guards.py -k account_persistent_profile -v`

Expected: FAIL，因当前 `init_browser()` 还没切到 persistent context

### Task 3: 最小实现 persistent profile 优先链路

**Files:**
- Modify: `utils/xianyu_slider_stealth.py`
- Modify: `XianyuAutoAsync.py`

- [ ] **Step 1: 在 `XianyuSliderStealth.__init__` 增加账号 persistent profile 开关与目录字段**

- [ ] **Step 2: 增加账号 profile 目录解析 helper，统一落到 `browser_data/user_<account_id>`**

- [ ] **Step 3: 改 `init_browser()`，在开关开启时优先 `launch_persistent_context(...)`**

- [ ] **Step 4: 仅在已知 profile 锁冲突时回退旧链路，避免直接炸死恢复流程**

- [ ] **Step 5: 在 `_handle_captcha_verification(...)` 创建 `XianyuSliderStealth` 时显式打开该选项**

### Task 4: 跑回归并补文档

**Files:**
- Modify: `README.md`
- Modify: `PASSWORD_LOGIN_VERIFICATION_FLOW_FIX_SUMMARY_20260504.md`
- Modify: `docs/superpowers/specs/2026-05-05-token-refresh-hydrated-profile-design.md`
- Modify: `docs/superpowers/plans/2026-05-05-token-refresh-hydrated-profile.md`

- [ ] **Step 1: 运行新增单测**

Run: `pytest tests/test_xianyu_token_refresh_request.py -k persistent_profile -v`

Expected: PASS

- [ ] **Step 2: 运行新增单测**

Run: `pytest tests/test_slider_verification_guards.py -k account_persistent_profile -v`

Expected: PASS

- [ ] **Step 3: 跑现有相关回归**

Run: `pytest tests/test_xianyu_token_refresh_request.py tests/test_slider_verification_guards.py tests/test_browser_cookie_warmup_verification_flow.py -v`

Expected: PASS

- [ ] **Step 4: 跑语法校验**

Run: `python -m py_compile XianyuAutoAsync.py utils\\xianyu_slider_stealth.py`

Expected: no output

- [ ] **Step 5: 更新文档**

记录：
- `token_refresh` 滑块恢复现在优先复用 `browser_data/user_<id>`
- 旧链路仍保留为 fallback
- hydrated profile 是稳定自动过块的关键前提，不是单纯轨迹参数
