# Token Refresh Stale Singleton Lock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `token_refresh` 复用账号级 persistent profile 时，只对可证明已失效的 Chromium singleton 锁做自动清理，避免误删真实占用锁。

**Architecture:** 保持现有 `use_account_persistent_profile` 主链不变，只在 `launch_persistent_context(...)` 命中已知 profile lock 错误时，读取 `SingletonLock` 目标，确认“当前宿主机 + PID 已不存在”后清理 `SingletonLock/SingletonCookie/SingletonSocket` 并仅重试一次。若无法证明 stale，继续沿用现有 fallback 到临时 context 链路。

**Tech Stack:** Python, unittest, Playwright sync API, apply_patch

---

### Task 1: 先补失败测试，锁定“安全清锁后重试”行为

**Files:**
- Modify: `tests/test_slider_verification_guards.py`

- [ ] **Step 1: 写失败测试，断言 `init_browser()` 命中 profile lock 且 helper 返回已清理时，会再次调用 `launch_persistent_context(...)`，而不是立刻回退 `launch(...)`**

- [ ] **Step 2: 运行测试确认当前实现会失败**

Run: `python -m unittest tests.test_slider_verification_guards.SliderVerificationGuardsTest.test_init_browser_retries_persistent_profile_after_stale_singleton_cleanup`

Expected: FAIL，因为当前实现还没有“一次清锁 + 一次重试”

### Task 2: 补保护测试，避免误删真实锁

**Files:**
- Modify: `tests/test_slider_verification_guards.py`

- [ ] **Step 1: 写测试，断言 `SingletonLock` 宿主机不匹配时不删除任何锁文件**

- [ ] **Step 2: 写测试，断言 `SingletonLock` 对应 PID 仍存活时不删除任何锁文件**

- [ ] **Step 3: 写测试，断言只有 `当前宿主机 + PID 已不存在` 时才删除 `SingletonLock/SingletonCookie/SingletonSocket`**

### Task 3: 最小实现安全清锁 helper

**Files:**
- Modify: `utils/xianyu_slider_stealth.py`

- [ ] **Step 1: 增加 singleton 锁解析 helper，读取 `SingletonLock` 的 symlink 目标并解析宿主机、PID**

- [ ] **Step 2: 增加 PID 存活判断 helper，优先使用 `os.kill(pid, 0)`，只把 `ProcessLookupError` 视为进程不存在**

- [ ] **Step 3: 增加安全清锁 helper，仅在满足以下条件时删除锁**

条件：
- 只处理 `SingletonLock` 为 symlink 的场景
- `SingletonLock` 目标能解析出 `host-pid`
- `host` 等于当前宿主机名
- `pid` 已不存在

- [ ] **Step 4: 无法证明 stale 时只打日志，不做删除**

### Task 4: 接入 `init_browser()` 的 persistent profile 分支

**Files:**
- Modify: `utils/xianyu_slider_stealth.py`

- [ ] **Step 1: 首次 `launch_persistent_context(...)` 命中 profile lock 时，调用安全清锁 helper**

- [ ] **Step 2: helper 返回已清锁时，仅重试一次 `launch_persistent_context(...)`**

- [ ] **Step 3: 重试仍是 lock 错误时，记录 warning 并回退旧临时 context 链路**

- [ ] **Step 4: 重试若变成非 lock 异常，则直接抛出，避免吞掉真正故障**

### Task 5: 验证并补文档

**Files:**
- Modify: `docs/superpowers/specs/2026-05-05-token-refresh-hydrated-profile-design.md`
- Modify: `PASSWORD_LOGIN_VERIFICATION_FLOW_FIX_SUMMARY_20260504.md`

- [ ] **Step 1: 运行相关单测**

Run: `python -m unittest tests.test_slider_verification_guards tests.test_xianyu_token_refresh_request`

Expected: PASS

- [ ] **Step 2: 运行语法检查**

Run: `python -m py_compile utils\\xianyu_slider_stealth.py XianyuAutoAsync.py`

Expected: no output

- [ ] **Step 3: 更新文档，明确“只清理可证明 stale 的 singleton 锁；无法证明 stale 时保持 fallback”**
