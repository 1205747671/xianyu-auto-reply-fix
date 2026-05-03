# 账号密码登录验证链路修复总结（2026-05-04）

## 结论

本轮把账号密码登录链路里最烦人的两类问题收了一遍：

1. **滑块后进入人脸/二维码验证时，截图路径刷新了但前端拿不到新图**
2. **验证页还没真正完成时，被过早误判成“登录成功”**

另外顺手把风控日志、截图回退、Cookie 预热接管、异步线程启动这些容易互相绊脚的地方也补齐了。

当前验证结果表明：

- 账密登录可以正常走到 **滑块 -> 人脸验证**
- 验证截图路径会持续刷新，不再卡死在旧图
- 半登录态不会被误判成成功 Cookie
- 远端 Linux 已同步并复测通过

---

## 本轮涉及文件

- `utils/xianyu_slider_stealth.py`
- `XianyuAutoAsync.py`
- `reply_server.py`

---

## 核心修改

### 1. `utils/xianyu_slider_stealth.py`

#### 1.1 验证截图刷新通知补齐

在 `_wait_for_context_login(...)` 中新增：

- `verification_screenshot_path`
- `last_verification_screenshot_path`

验证页变化判定不再只看：

- `verification_type`
- `verification_url`

现在还会比较：

- `verification_screenshot_path`
- `recovered_from_timeout`

这样即使验证类型和 URL 没变，只要**截图路径变了**，也会重新通知前端。

#### 1.2 超时/失效验证页不再覆盖成废图

`_capture_verification_screenshot(...)` 现在会先识别页面文本：

- 如果当前验证页已经进入“超时/失效态”
- 且目录里已经有上一张可用验证截图

则直接复用上一张可用图，不再把“超时页”覆盖成当前展示图。

#### 1.3 超时页恢复逻辑补齐

在 `_process_verification_requirement(...)` 和 `_wait_for_context_login(...)` 里补了：

- 超时验证页识别
- 恢复入口点击
- 恢复后的二维码/验证页重新接管

这样“超时页”不再一刀切误处理，有恢复入口时会继续走恢复链路。

#### 1.4 登录成功判定收紧

`_probe_context_login_success(...)` 不再因为页面元素命中就直接判成功。

现在同时要求：

- Cookie 完整
- URL 处于已登录页
- 页面上没有滑块
- 页面不像验证页
- 不存在 pending identity markers

避免“看着像进去了，其实还在人脸验证里”的误判。

#### 1.5 成功 Cookie 快照清理 pending identity markers

`_finalize_logged_in_cookies(...)` 返回成功 Cookie 前，会清理：

- `ivActionType`
- `tmp0`
- `siv20`
- `last_u_xianyu_web`

防止后续链路继续把这类“待确认验证标记”当成未完成状态。

#### 1.6 浏览器 Cookie 预热增强

新增/增强了：

- `XY_BROWSER_COOKIE_WARMUP_TIMEOUT_MS`
- `last_browser_cookie_warmup_verification_hint`
- `request.post` 优先的预热探测
- `Set-Cookie` 补充合并
- 预热返回验证入口后的接管逻辑

这样服务端返回 `FAIL_SYS_USER_VALIDATE` / `identity_verify` / `punish` 一类信息时，不再只是傻等，而是可以把验证页接起来继续处理。

#### 1.7 账密登录完成后的 Cookie 收口统一

密码登录成功后的 Cookie 获取不再现场拼一大坨临时代码，改为统一走：

- `_finalize_logged_in_cookies(...)`

把：

- Set-Cookie 合并
- Cookie 稳定化
- 预热补齐
- pending identity 检查
- 最终清理

收成一条标准链。

#### 1.8 同步登录方法改为新线程启动

新增：

- `_run_sync_method_on_fresh_thread(...)`

并用于异步侧调用同步 Playwright 登录逻辑，目的是尽量规避：

- `Cannot switch to a different thread`

这个问题还没完全消失，但线程边界已经更规整了。

---

### 2. `XianyuAutoAsync.py`

把原来的：

- `await asyncio.to_thread(...)`

改成：

- `await slider._run_sync_method_on_fresh_thread(...)`

用于调用 `slider.login_with_password_playwright(...)`。

这和上面的 `utils` 配套，主要是减少 greenlet / thread mismatch。

---

### 3. `reply_server.py`

#### 3.1 账密登录失败统一收口

新增：

- `_is_password_login_verification_timeout_message(...)`
- `_derive_password_login_verification_failure_result_code(...)`
- `_finalize_password_login_session_failure(...)`

把验证超时、失效、失败这些场景统一归口，避免各处分散写状态。

#### 3.2 待处理验证风控日志自动收口

新增：

- `_close_password_login_pending_verification_risk_logs(...)`

在以下场景收口遗留 `processing` 风控日志：

- 登录成功
- 登录失败
- 用户取消

避免后台一直挂着“处理中”。

#### 3.3 验证截图接口优先返回当前会话截图

新增：

- `_get_latest_password_login_session_for_account(...)`
- `_get_latest_verification_risk_log_for_account(...)`
- `_is_timed_out_verification_risk_log(...)`
- `_build_face_verification_screenshot_info(...)`

`/get_account_face_verification_screenshot` 相关逻辑现在优先：

1. 当前登录会话的截图
2. 当前会话失败且已超时，则直接拒绝历史图回退
3. 风控日志显示最近验证已超时，也不再回退到旧图

这样就不会出现：

- 实际当前验证已经超时
- 页面却还弹一张几小时前老图

#### 3.4 失效验证页直接标记失败

当登录会话检测到：

- 已超时/失效
- 且需要重新发起验证

会直接把会话状态打成失败，不再假装还在继续处理中。

---

## 实际验证

### 本地验证

#### 语法检查

```powershell
python -m py_compile XianyuAutoAsync.py reply_server.py utils\xianyu_slider_stealth.py
```

结果：通过

#### 单测

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_browser_cookie_warmup_verification_flow tests.test_reply_server_password_login_timeout_flow
```

结果：`26 tests OK`

### 本地真实账密复现

使用：

```powershell
.\.venv\Scripts\python.exe -u debug_manual_password_login.py --account-id local_pwd_debug_acc1_20260504a --account 212225791@qq.com --password qwe1205747671 --headless --force-clean-context --max-retries 1 --verification-wait-timeout 20 --keep-verification-screenshot
```

结果确认：

- 可以正常到滑块
- 滑块自动成功
- 后续进入 `face_verify`
- 截图路径会刷新
- 最终失败原因只是**人工未在超时前完成人脸验证**

### 远端 Linux 验证

远端路径：

- `/mnt/d/Sof/xianyu-auto-reply-fix/new/xianyu-auto-reply-fix-main`

已同步代码并备份原文件后复测。

远端实测结果：

- 滑块成功
- 后续进入 `face_verify`
- 截图路径连续刷新，例如：
  - `...004920.jpg`
  - `...004936.jpg`
  - `...005000.jpg`
  - `...005024.jpg`
- 日志里明确出现多次：
  - `验证等待期间检测到验证页变化`
  - `准备发送验证通知，截图路径: ...`

说明远端“老图不刷新”的核心问题已经修掉。

---

## 当前还没收掉的尾巴

### 1. 账号编辑弹窗无改动保存仍会触发代理更新重启

现象：

- `POST /cookie/{cid}/account-info` 本身没问题
- 但前端随后还会 `POST /cookie/{cid}/proxy`
- 即使代理仍是 `none/空/0`，后端也会重启账号任务

这个问题**本轮未修**。

### 2. Playwright 关闭阶段仍偶发线程清理告警

仍可能出现：

- `Cannot switch to a different thread`

当前看不影响主流程修复结论，但资源清理链路还不算完全漂亮。

---

## 最终判断

这轮核心修复已经把“验证截图不刷新 / 老图回退 / 半登录态误判成功 / 风控日志一直处理中”这些关键问题打通了。

当前系统剩余问题主要是：

- 编辑账号时的无效重启
- Playwright 清理阶段线程告警

它们是后续优化项，不影响本轮核心修复已经成立。
