# 闲鱼账号认证 / Cookie / 保活链路修复总日志（截至 2026-05-05）

## 结论

截至 **2026-05-05**，这套账号链路可以明确分成几块来看：

1. **登录前半段已经打通**
   - 手动 Cookie 导入：正式前端、正式接口、真实浏览器验证链路已经对齐。
   - 账号密码登录：无头登录页、自动滑块、后续验证页接管已经打通。

2. **登录后半段仍受账号风控影响**
   - 某些账号会在滑块后进入二维码/人脸验证。
   - 这不等于“滑块没过”，也不等于“账密链路坏了”，很多时候就是账号本身被要求继续做人工验证。

3. **验证页承接这块已经收得比之前规整多了**
   - 验证截图会随着当前会话持续刷新，不再老拿历史旧图糊弄前端。
   - 超时/失效页不会再把可用验证图覆盖成废图。
   - 半登录态不会再被误判成成功。
   - 风控日志、登录会话、验证截图接口的状态收口更一致。

4. **保活 / `token_refresh` 这条恢复链也已经补上关键收口**
   - Token 刷新会优先沿用当前内存里的新认证 Cookie 和当前账号代理。
   - 命中 `idlemessage.pc.login.token -> punish` 时，不再把处罚页壳子当真滑块死磕。
   - 一旦出现“验证失败，点击框体重试(error:xxxx)”这类硬拒绝，当前滑块流程会尽快结束并回退到账密恢复。
   - **2026-05-05 补充**：`token_refresh` 自动滑块现在会优先复用 `browser_data/user_<account_id>` 的账号级 hydrated persistent profile；只有 profile 被占用时，才回退到旧的临时上下文链路。
   - **多账号边界**：每个账号只会碰自己的 `browser_data/user_<account_id>`，不会共用浏览器状态；同账号已有滑块任务在跑时，沿用现有并发保护排队，不会自己抢自己的 profile。
   - **影响范围边界**：这次 persistent profile 优先逻辑只挂在 `token_refresh` 的自动滑块恢复链上，不会把正常消息监听、订单处理、手动导入 Cookie 等主链统一切到这套路径。
   - **2026-05-05 stale lock 补充**：如果账号级 profile 里残留的是“当前宿主机 + 已失效 PID”的 Chromium `SingletonLock`，或 Docker 容器 hostname 漂移留下的旧容器 ID 锁，程序会先安全清理 `SingletonLock` / `SingletonCookie` / `SingletonSocket`，然后只重试一次 persistent context；拿不准就不删，继续走旧 fallback。

5. **2026-05-05 已把“本地通、Linux 不通”的运行时差异钉死并对齐**
   - 根因不是源码逻辑跑偏，而是 Linux 容器里的项目浏览器仍落在 `/ms-playwright/chromium-1208/...`，实际画像还是 Chrome 145。
   - 本地成功环境实际是 `playwright 1.59.0 + chromium-1217 / Chrome 147`，所以把 `requirements.txt` 锁到 `playwright==1.59.0` 后，远端 Docker 也回到了同一套画像。
   - 2026-05-05 02:02 远端账号 1 已重新稳定跑到 `face_verify`，截图 `face_verify_1_20260505_020248.jpg`、`face_verify_1_20260505_020259.jpg` 持续刷新。
   - **结合 2026-05-05 02:06 之后的远端日志推断**，用户手动完成人脸后账号 1 已重新进入正常运行态：心跳持续正常、`GET /api/orders` 成功返回 6 条记录、前端连续拉取 `cookies/details` / `items` / `keywords` 都正常。

一句话说透：

> 现在真正要盯的是“滑块后的验证承接和账号风控状态”，不是再回头拿滑块当背锅侠。

---

## 本文整合来源

这份文档已经把下面这些历史资料和最近相关提交一起并进来了，后续**根目录只保留本文**作为账号链路修复日志入口：

- `SLIDER_HEADLESS_FIX_SUMMARY_20260501.md`
- `PASSWORD_LOGIN_HEADLESS_FIX_SUMMARY_20260501.md`
- `FORMAL_COOKIE_PASSWORD_FLOW_ALIGNMENT_20260501.md`
- `PASSWORD_LOGIN_VERIFICATION_FLOW_FIX_SUMMARY_20260504.md`
- `c3f74db` `fix refresh preflight timeout and slot ownership cleanup`
- `247070d` `fix password verification refresh and timeout handling`
- `2a2d07e` `fix manual cookie import verification precheck`
- `216168a` `fix token refresh session proxy handling`
- `1ebe946` `fix token refresh punish fallback`

老文档里的有效信息已经吸进本文，旧账号修复文档也已经从根目录移走，别再翻四五份看得脑瓜子疼。

---

## 一、时间线与演进结论

### 2026-05-01：先把无头滑块和正式入口对齐

主要结论：

- 手动 Cookie 导入不能再只是“把 Cookie 存库就完事”。
- 正式前端和 debug 脚本不能各走各的野路子，必须接到同一条真实浏览器验证链路。
- 无头滑块默认策略改为优先走 `playwright`，而不是继续让 `patchright` 当默认。

### 2026-05-02：再把无头账密链路真正打通

主要结论：

- 账号密码登录页不能粗暴套 `full stealth`，否则前端直接白屏或表单异常。
- 账密链路要拆阶段：
  - **登录页：`lite stealth`**
  - **滑块页：`full runtime stealth`**
- 正式 `/password-login` 链路已经可以稳定跑到：

```text
账号密码 -> 无头滑块成功 -> 二维码/人脸后续验证
```

### 2026-05-03：补手动刷新预检超时和并发槽位清理

主要结论：

- 刷新模式下，Token 预检不能无限等，否则会话容易一直吊在处理中。
- 并发槽位释放不能只看账号 ID，不看实例归属，不然旧实例清理时可能把新实例槽位一块儿放飞。
- `close_browser()` 这类清理链路需要优先释放槽位并做超时保护，避免后续账号一直排队。

### 2026-05-04：把验证页刷新、超时恢复、状态收口补齐

主要结论：

- 老图不刷新的根因被确认并修掉。
- 超时页、失效页、待处理风控日志、半登录误判成功这些脏边角，被集中收口。

### 2026-05-04 晚间补充：把 `token_refresh / punish / 保活恢复` 这条链收住

主要结论：

- 手动 Cookie 正式导入增加了 `verification_url` 预检，避免“假导入成功，后面后台自己撞风控”。
- `token_refresh` 会优先使用**当前内存中的新认证 Cookie + 当前账号代理**，避免恢复链路拿错代理或旧 Cookie。
- `token_refresh -> mtop.taobao.idlemessage.pc.login.token -> punish` 这类处罚页，不再被当成 fresh login 真滑块死磕。
- 如果页面命中：
  - `验证失败，点击框体重试`
  - 且带 `fail_code / error:xxxx`
  - 并且页面实际上没有可操作滑块
  那就**第一次失败直接收口**，并交给外层走账密恢复。
- `run()` 失败后不再继续把这种处罚页接力成二维码/验证页二次接管，避免原地鬼打墙。

### 2026-05-05：把本地与 Linux 远端运行时彻底对齐

主要结论：

- 这次 Linux “账号密码登录又过不去滑块” 的根因，不是代码逻辑没同步，而是**项目实际使用的 Playwright 浏览器版本没同步**。
- 远端容器系统自带 `/usr/bin/chromium` 虽然已经是 `147.0.7727.55`，但账密链路会优先走：
  - `/ms-playwright/chromium-1208/chrome-linux64/chrome`
  - 实际版本还是 `145.0.7632.6`
- 本地成功环境对应的是：
  - `playwright 1.59.0`
  - `.playwright-browsers/chromium-1217`
  - `Chrome 147`
- 因此本轮把 `requirements.txt` 从：
  - `playwright>=1.40.0`
  改成：
  - `playwright==1.59.0`
  然后重建远端 Docker。
- 重建后远端项目浏览器回到：
  - `chromium-1217`
  - `Google Chrome for Testing 147.0.7727.15`
- 2026-05-05 02:02，账号 1 在远端重新实测：
  - 第 2 次尝试滑块成功；
  - 检测到 `face_verify`；
  - 截图 `face_verify_1_20260505_020248.jpg`、`face_verify_1_20260505_020259.jpg` 持续刷新。
- **结合 2026-05-05 02:06 之后的线上日志推断**，用户手动完成人脸后账号 1 已恢复：
  - 心跳持续正常；
  - `GET /api/orders` 成功返回 6 条记录；
  - 前端连续读取 `cookies/details`、`keywords`、`items` 正常。

---

## 二、现在的正式链路长啥样

### 1. 手动 Cookie 导入正式链路

正式接口：

- `POST /manual-cookie-import`
- `GET /manual-cookie-import/check/{session_id}`

正式行为：

1. 前端提交账号 ID + Cookie。
2. 后端先根据 Cookie 预检最新 `verification_url`。
3. 再调用 `XianyuSliderStealth.run(...)` 跑一次**真实浏览器验证链路**。
4. 验证成功后，把浏览器拿回来的 Cookie 和原始 Cookie 做保护性合并。
5. 最后才写库，并更新 `cookie_manager`。

补充说明：

- 当前主流程重点是“真实浏览器滑块验证 -> 成功/失败收口”。
- 前端和状态接口已经按异步会话方式接好了轮询。
- 代码里虽然预留了 `verification_required` 结构，但手动 Cookie 导入主流程当前**没有像账密登录那样显式抛出人工验证态**。

这意味着：

- **不是先存脏 Cookie 再让后台无限重试**。
- **不是 debug 能跑，正式入口却半残**。
- **正式前端自己就能接住这条导入会话的状态轮询**。

### 2. 账号密码登录正式链路

正式接口：

- `POST /password-login`
- `GET /password-login/check/{session_id}`
- `POST /password-login/cancel/{session_id}`
- `GET /face-verification/screenshot/{account_id}`

正式行为：

1. 无头真实浏览器打开闲鱼登录页。
2. 自动进入账密登录表单。
3. 自动填写账号密码、勾协议、提交。
4. 登录页阶段使用 `lite stealth`，避免白屏和表单失效。
5. 检测到滑块后，切换到 `full runtime stealth` + 网络层伪装继续跑。
6. 如果账号命中二维码/人脸验证，则向前端抛出验证截图、验证类型、会话状态。
7. 如果最终登录成功，则统一走 `_finalize_logged_in_cookies(...)` 收口 Cookie。

补充一点：

- 当前正式前端 / 接口已经支持 `cancelled` 终态，用户可以主动取消正在等待人工验证的会话。

当前正确认知应该是：

```text
账密链路卡在二维码/人脸验证
≠ 账密错误
≠ 滑块失败
≠ 前半段链路没通
```

### 3. 保活 / `token_refresh` / 浏览器 Cookie 刷新恢复链路

核心入口：

- `_refresh_token_impl(...)`
- `_handle_captcha_verification(...)`
- `_try_password_login_refresh(...)`
- `_refresh_cookies_via_browser(...)`

正式行为：

1. 实例周期性进入 `token_refresh`，优先用**当前内存里的认证 Cookie**和**当前账号代理**请求：
   - `mtop.taobao.idlemessage.pc.login.token`
2. 如果 Token 刷新成功：
   - 重置消息接收时间标识；
   - 必要时继续做浏览器 Cookie 稳定化 / 业务预热。
3. 如果服务端返回：
   - `FAIL_SYS_USER_VALIDATE`
   - `identity_verify`
   - `punish`
   则进入 `_handle_captcha_verification(...)`，创建 `XianyuSliderStealth` 接管验证。
4. 这条链会显式打上：
   - `risk_trigger_scene = 'token_refresh'`
   用来把它和 fresh login / 手动 Cookie / 账密滑块区分开。
5. `token_refresh` 滑块接管时，会优先复用：
   - `browser_data/user_<account_id>`
   这套账号级持久浏览器目录，把已有 Cookie、localStorage、站点状态和历史风控信任一起带上。
   每个账号只会使用自己的目录，不会跨账号共用；同账号如果已有滑块任务占着槽位，也会先走现有并发保护而不是再起一个实例抢同一个 profile。
   如果只是 profile 锁冲突，才会回退到旧的 `launch(...) + new_context() + add_cookies()` 临时上下文兜底。
   如果锁冲突能进一步证明只是当前宿主机残留的 stale Chromium singleton 锁，则会先安全清理锁文件，再重试一次 persistent context。
   这套 persistent profile 优先逻辑只作用在 `token_refresh` 这条恢复链，不会把正常消息监听、订单处理、手动导入 Cookie 主链整体改成持久 profile 模式。
6. 如果页面其实只是处罚页壳子，且出现：
   - `验证失败，点击框体重试(error:xxxx)`
   - 没有真实滑块按钮/轨道
   那就**第一次失败直接停止当前滑块重试**，也不再继续二维码页二次接管。
7. 外层随后回退：
   - `_try_password_login_refresh(...)`
   重新拿一份可用认证态，再继续保活。
8. 后续 `_refresh_cookies_via_browser(...)` 会补：
   - 受保护 Cookie 合并
   - 网页登录态校验
   - 图片上传 API 校验
   避免拿一坨半残 Cookie 继续跑。

---

## 三、几个必须记住的注意事项

### 1. 登录页和滑块页不能一锅炖

这是之前最坑的点。

- 登录页上 `full stealth`，容易把前端事件系统搞坏。
- 滑块页如果还维持 `lite stealth`，又容易被风控狠狠干趴。

所以现在的策略必须分开：

- **登录页：`lite`**
- **滑块页：`full runtime`**

### 2. 手动 Cookie 导入必须先验证再落库

以前那种“先存再说”的玩法，结果就是：

- 前端以为导入成功了；
- 后台其实还在那死循环撞滑块；
- 真问题全被埋了。

现在必须是：

> 真实浏览器验证通过 -> 保护性合并 Cookie -> 再写正式账号

### 3. 验证截图必须跟当前会话走，不能拿老图回退

前面出过的真实问题就是：

- 新验证图其实已经保存了；
- 但前端接口还在回历史旧图；
- 用户看见的是“超时”或几小时前的截图；
- 实际当前会话信息全错位。

现在这块已经按“当前会话优先”重做了。

### 4. `token_refresh` 的处罚页别当真滑块死磕

这块最近最容易把人带沟里。

- `...login.token/.../punish?action=captcha&pureCaptcha=` 更多是**保活恢复场景**，不是 fresh login。
- 页面上看到 `#nocaptcha`、`.sm-btn-wrapper` 这种壳子，不等于就还有真滑块给你拖。
- 如果页面只有处罚壳，没有真实按钮/轨道，再怎么重试也基本是在给风控做俯卧撑。

这种场景正确处理是：

> 尽快结束当前滑块流程，回退到账密恢复，而不是在处罚页原地绕圈。

### 5. 半登录态不能当成功

只看到某个页面元素、某个 URL、某张 Cookie 快照，就直接宣布成功，这种事之前干过，结论就是坑人。

现在成功判定会同时看：

- Cookie 是否完整；
- URL 是否处于已登录页；
- 页面上是否还在滑块态；
- 页面是否仍像验证页；
- 是否还残留 `pending identity markers`。

---

## 四、无头 / 滑块 / 反检测策略的统一口径

### 1. 手动 Cookie 滑块链路

当前默认口径：

- 自动化后端优先 `playwright`
- 无头场景默认优先走项目内 Playwright 浏览器
- `stealth_mode` 默认按运行环境自动解析

当前代码里的实际默认分支是：

- 常规场景：解析到 `full`
- `headless + docker + playwright` 且**没有锁定到项目内浏览器缓存 / 显式浏览器路径**时：会保守降到 `lite`

关键原因：

- `patchright + headless` 有成功样本，但波动大，不适合继续当默认。
- `playwright + headless` 这条线更稳。
- Docker 场景如果落到系统兜底浏览器，`full` 改写有时会把风控打醒，所以代码里保留了自动保守降级。

### 2. 账号密码登录链路

当前默认口径：

- 外层仍是 `playwright + headless + auto`
- 但 `login_with_password_playwright()` 内部会按页面阶段自动拆成：
  - 登录页：`lite`
  - 滑块页：`full runtime`

### 3. 浏览器来源

现在优先复用项目内浏览器：

- `.playwright-browsers/`

这样做的意义很直接：

- 不依赖本机外部浏览器安装状态；
- 正式环境和调试环境更容易对齐；
- Docker / Linux / Windows 的行为边界更好控。
- 而且项目内浏览器一旦锁定成功，Cookie 滑块链路通常也会回到更积极的 `full stealth` 策略。

### 4. 网络层伪装不是摆设

账密链路后来补强过的关键点包括：

- `Network.setUserAgentOverride`
- `userAgentMetadata`
- `sec-ch-ua`
- `sec-ch-ua-platform`
- `navigator.userAgentData`

要不然就是运行时指纹和网络层指纹对不上，风控直接给你上强度。

---

## 五、2026-05-04 这轮最新修复到底改了啥

### 1. `utils/xianyu_slider_stealth.py`

#### 1.1 验证截图路径纳入“验证变化”判定

在 `_wait_for_context_login(...)` 里新增并使用：

- `verification_screenshot_path`
- `last_verification_screenshot_path`

现在即使：

- `verification_type` 没变
- `verification_url` 没变

只要**截图路径变了**，也会重新通知前端。

这就是“老图不刷新”被修掉的关键。

#### 1.2 超时/失效页不再覆盖可用验证图

`_capture_verification_screenshot(...)` 现在会先识别页面是否已经进入超时/失效态。

如果：

- 当前页已经超时；
- 目录里已经有上一张可用验证图；

那就直接复用旧的可用验证图，不再拿超时页把展示图砸烂。

#### 1.3 超时页恢复链路补齐

在 `_process_verification_requirement(...)` 和 `_wait_for_context_login(...)` 里补了：

- 超时页识别；
- 恢复入口点击；
- 恢复后的验证页重新接管。

也就是说，超时页现在不是一刀切判死，有恢复入口时会继续往下走。

#### 1.4 登录成功判定收紧

`_probe_context_login_success(...)` 不再因为局部页面命中就直接宣布成功。

现在要同时满足：

- Cookie 完整；
- URL 看起来处于已登录页；
- 页面上没有滑块；
- 页面不像验证页；
- 不存在待确认身份标记。

#### 1.5 统一清理 pending identity markers

`_finalize_logged_in_cookies(...)` 会在返回成功 Cookie 前清理：

- `ivActionType`
- `tmp0`
- `siv20`
- `last_u_xianyu_web`

避免后续链路继续把这些残留标记误判成“还没验证完”。

#### 1.6 浏览器 Cookie 预热接管增强

这轮补强了：

- `XY_BROWSER_COOKIE_WARMUP_TIMEOUT_MS`
- `last_browser_cookie_warmup_verification_hint`
- `request.post` 优先的预热探测
- `Set-Cookie` 补充合并
- 预热返回验证入口后的接管逻辑

这样服务端如果回：

- `FAIL_SYS_USER_VALIDATE`
- `identity_verify`
- `punish`

就不再只是傻等，而是能把验证页接起来继续处理。

#### 1.7 同步 Playwright 登录改为新线程启动

新增：

- `_run_sync_method_on_fresh_thread(...)`

并在异步侧用它替换部分 `asyncio.to_thread(...)` 调用，用来尽量规避：

- `Cannot switch to a different thread`

这警告还没完全绝种，但边界已经比之前规整。

#### 1.8 手动刷新预检和并发槽位清理补强

这一块对应 `c3f74db` 这笔修复，当前代码里已经能看到：

- 手动刷新模式的 Token 预检增加了超时控制；
- 并发槽位释放改成“账号 + 实例归属”双重校验；
- `close_browser()` 会优先释放槽位，并对 `playwright.stop()` 做超时保护；
- 密码登录 / 手动 Cookie 导入结束时，都会尽量避免误释放别的活跃实例槽位。

这块不直接影响“截图刷不刷新”，但会影响：

- 会话是不是一直卡在处理中；
- 后续账号任务会不会因为槽位泄漏一直排队；
- 清理旧实例时会不会误伤新实例。

#### 1.9 `token_refresh` 处罚页快速收口

这块对应最近的保活恢复补丁，关键点是：

- `token_refresh` 场景创建 `XianyuSliderStealth` 时会显式标记：
  - `risk_trigger_scene = 'token_refresh'`
- 新增：
  - `_should_abort_token_refresh_slider_flow_after_failure(...)`
- 判定口径不再只盯某一个错误码，而是统一看：
  - `验证失败，点击框体重试`
  - `fail_code`
  - `dom_error_text / error:xxxx`
- 远端已实际见过的失败码包括：
  - `4zgv4`
  - `a2904`
  - `bry4N4`
  - `RuZU44`
  - `YN1D4`
- 命中这类 hard reject 后：
  - `solve_slider()` 第一次失败就停止重试；
  - `run()` 不再继续 `_detect_qr_code_verification()` 二次接管；
  - 外层尽快回退到账密恢复。

### 2. `reply_server.py`

这轮重点补了 4 块：

1. **统一失败收口**
   - `_is_password_login_verification_timeout_message(...)`
   - `_derive_password_login_verification_failure_result_code(...)`
   - `_finalize_password_login_session_failure(...)`

2. **待处理风控日志自动收口**
   - `_close_password_login_pending_verification_risk_logs(...)`

3. **验证截图接口按当前会话优先**
   - `_get_latest_password_login_session_for_account(...)`
   - `_get_latest_verification_risk_log_for_account(...)`
   - `_is_timed_out_verification_risk_log(...)`
   - `_build_face_verification_screenshot_info(...)`

4. **失效验证页直接打失败**
   - 当前会话已经超时/失效，且需要重新发起验证时，不再假装还在处理中。

### 3. `XianyuAutoAsync.py`

把部分：

- `await asyncio.to_thread(...)`

换成：

- `await slider._run_sync_method_on_fresh_thread(...)`

这不是花活，是为了减少 greenlet / thread mismatch。

另外，保活恢复这条链补了两件事：

- `_handle_captcha_verification(...)` 创建滑块实例时，会显式标记：
  - `risk_trigger_scene = 'token_refresh'`
- `token_refresh` 碰到处罚页 hard reject 后，会尽快退出当前滑块链，把恢复权交回：
  - `_try_password_login_refresh(...)`

---

## 六、本地与远端验证证据

### 1. 本地静态验证

语法检查：

```powershell
python -m py_compile XianyuAutoAsync.py reply_server.py utils\xianyu_slider_stealth.py
```

结果：通过

单测：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_browser_cookie_warmup_verification_flow tests.test_reply_server_password_login_timeout_flow
```

结果：`26 tests OK`

### 2. 手动 Cookie 正式链路验证

已验证过：

- `POST /manual-cookie-import` 正式接口可成功落库；
- 正式前端“导入并验证账号”流程可成功轮询收口；
- 新实例可接管成功 Cookie 并进入正常运行态。

### 3. 账号密码正式链路验证

已验证过：

- 登录页可正常渲染；
- 账密表单可正常输入与提交；
- 滑块可自动通过；
- 后续可进入二维码/人脸验证；
- 正式前端与接口都能接住验证截图与会话轮询。

### 4. 2026-05-04 最新回归结果

本轮已确认：

- 滑块后进入 `face_verify`；
- 验证截图路径会持续刷新；
- 前端不再死盯旧截图；
- 半登录态不再误判成成功；
- Linux 远端同步后复测通过。

远端验证路径：

- `/mnt/d/Sof/xianyu-auto-reply-fix/new/xianyu-auto-reply-fix-main`

典型日志证据包括：

- `检测到验证等待期间检测到验证页变化`
- `准备发送验证通知，截图路径: ...`
- 连续变化的截图文件名，例如 `...004920.jpg`、`...004936.jpg`、`...005000.jpg`

### 5. `token_refresh punish` 远端实测证据

这轮远端已经抓到过多份 `logs/slider_debug/*.json`，共同特征很统一：

- 页面标题：
  - `验证码拦截`
- URL：
  - `.../mtop.taobao.idlemessage.pc.login.token/.../punish?...&action=captcha&pureCaptcha=`
- 运行时特征：
  - `hasNocaptcha=true`
  - `hasSliderButton=false`
  - `hasSliderTrack=false`
- 失败文案：
  - `验证失败，点击框体重试(error:xxxx)`

这说明这玩意儿很多时候根本不是“还能继续拖的真滑块”，而是处罚页壳子。

远端已经实测到的错误码包括：

- `4zgv4`
- `a2904`
- `bry4N4`
- `RuZU44`
- `YN1D4`

所以这轮修复才会把规则从“只盯 `4zgv4`”升级成“`token_refresh` 场景下，凡是命中固定失败文案 + 错误码的处罚页，都尽快收口”。

### 6. 2026-05-05 账密 / 人脸 / 线上恢复补充证据

本地直接调试命令：

```powershell
.\.venv\Scripts\python.exe .\debug_manual_password_login.py --account-id 1 --account 212225791@qq.com --password qwe1205747671 --force-clean-context --automation-backend playwright --max-retries 4 --verification-wait-timeout 25 --keep-verification-screenshot
```

本地结论：

- 滑块已成功；
- 后续稳定进入 `face_verify`；
- 由于本地没做人脸，25 秒超时退出是预期行为，不是链路失败。

远端 2026-05-05 02:02 关键日志：

- `【1】✅ 滑块验证成功！`
- `【1】检测到验证类型: face_verify`
- `【1】等待二维码/人脸验证完成... (timeout=450s)`
- `【1】✅ 验证截图已保存: static/uploads/images/face_verify_1_20260505_020248.jpg`
- `【1】✅ 验证截图已保存: static/uploads/images/face_verify_1_20260505_020259.jpg`

远端 2026-05-05 02:06 之后运行态证据：

- `【1】心跳响应正常`
- `【admin#1】用户订单查询成功，共 6 条记录`
- `GET /cookies/details`、`GET /keywords-with-item-id/1`、`GET /items/1` 均持续返回 `200`

这组证据连起来说明两件事：

1. **前半段账密 + 滑块链路已经重新稳定。**
2. **结合后续心跳和订单查询日志推断，用户手动完成人脸后账号 1 已重新回到可用态。**

---

## 七、调试脚本怎么用

这两个脚本现在都已经入库，并且已经去掉明文账号密码：

- `debug_manual_cookie_slider.py`
- `debug_manual_password_login.py`

### 1. Cookie 调试

适合确认：

- 当前 Cookie 是否会触发滑块；
- 默认无头滑块策略是否稳定；
- 浏览器返回 Cookie 是否完整。

示例：

```powershell
.\.venv\Scripts\python.exe -u debug_manual_cookie_slider.py --cookie "<cookie>" --cookie-id debug_cookie --headless --max-retries 1
```

### 2. 账密调试

适合确认：

- 是否能正常进账密表单；
- 滑块是否能自动通过；
- 后续到底是二维码、人脸，还是别的风控页；
- 当前截图路径和验证类型是不是最新会话的。

示例：

```powershell
.\.venv\Scripts\python.exe -u debug_manual_password_login.py --account-id debug_pwd --account "<account>" --password "<password>" --headless --force-clean-context --max-retries 1 --verification-wait-timeout 20 --keep-verification-screenshot
```

注意：

- `--verification-wait-timeout` 设很短，只适合复现问题，不适合真等人工验证。
- 真要等手机扫码或做人脸，应该交给正式前端会话轮询去接管。

---

## 八、当前仍未收口的问题

### 1. 编辑账号时的无效重启

现象：

- `POST /cookie/{cid}/account-info` 本身没问题；
- 但前端后面还会跟一发 `POST /cookie/{cid}/proxy`；
- 即使代理仍是 `none/空/0`，后端也可能重启账号任务。

这个问题**不是本轮验证链路修复引入的**，而且当前**尚未处理**。

### 2. Playwright 关闭阶段偶发线程告警

仍可能看到：

- `Cannot switch to a different thread`

当前判断：

- 不影响本轮核心结论；
- 但资源清理链路还不算完全漂亮，后续值得继续收。

---

## 九、涉及文件一览

本轮整理涉及的核心代码与文档入口如下：

- `utils/xianyu_slider_stealth.py`
- `XianyuAutoAsync.py`
- `reply_server.py`
- `utils/xianyu_utils.py`
- `requirements.txt`
- `static/js/app.js`
- `static/index.html`
- `debug_manual_cookie_slider.py`
- `debug_manual_password_login.py`
- `tests/test_manual_cookie_import_precheck.py`
- `tests/test_reply_server_manual_cookie_import_flow.py`
- `tests/test_xianyu_token_refresh_request.py`
- `.gitignore`
- `AGENTS.md`

已并入本文、并已从根目录移走的历史文档：

- `SLIDER_HEADLESS_FIX_SUMMARY_20260501.md`
- `PASSWORD_LOGIN_HEADLESS_FIX_SUMMARY_20260501.md`
- `FORMAL_COOKIE_PASSWORD_FLOW_ALIGNMENT_20260501.md`

---

## 十、最近相关提交（已并入本文）

按时间顺序看，最近跟账号链路直接相关的提交大致是这些：

| 日期 | Commit | 说明 |
| --- | --- | --- |
| 2026-05-03 | `c3f74db` | 手动刷新预检加超时，并发槽位释放增加实例归属校验，避免会话卡死和误释放。 |
| 2026-05-04 | `247070d` | 验证截图刷新、超时页恢复、半登录态判定、风控日志和会话状态收口补齐。 |
| 2026-05-04 | `143790c` | 把之前分散的账号链路文档先收成一个统一入口。 |
| 2026-05-04 | `2a2d07e` | 手动 Cookie 正式导入加 `verification_url` 预检，避免假成功。 |
| 2026-05-04 | `216168a` | `token_refresh` 优先使用当前内存认证 Cookie 和当前代理，避免恢复链路拿错上下文。 |
| 2026-05-04 | `1ebe946` | `token_refresh -> punish` 命中处罚页 hard reject 时，第一次失败直接收口并回退到账密恢复。 |

## 最终判断

这几轮修改串起来看，现阶段已经能比较明确地下结论：

1. **手动 Cookie 导入正式链路已经不是假把式。**
2. **无头账密登录的前半段（登录页 + 滑块）已经打通。**
3. **当前主要矛盾已经转移到滑块后的人工验证承接与账号风控本身。**
4. **2026-05-04 这轮修复已经把“老图不刷新、超时页乱回退、半登录误判成功、风控日志一直处理中”这几类关键坑补上。**
5. **`token_refresh` 场景下的处罚页死循环，也已经从“反复撞滑块”改成“识别硬拒绝后尽快回退到账密恢复”。**
6. **2026-05-05 已确认 Linux 远端的真正问题是 Playwright/Chromium 运行时版本落后；对齐到 `playwright 1.59.0 + chromium-1217 / Chrome 147` 后，账号 1 已重新打通到人脸验证，并在用户手动处理后恢复线上可用。**

所以后面如果再看见账号卡住，优先排查顺序应该是：

1. 当前会话到底是不是已经进入二维码/人脸验证；
2. 前端拿到的截图是不是当前会话最新截图；
3. 验证页是否已经超时或失效；
4. 账号本身是否继续命中风控；
5. 最后才轮到回头怀疑滑块或账密前半段。

别再逮着滑块一顿猛锤了，前半段现在大体已经不是主要矛盾。
