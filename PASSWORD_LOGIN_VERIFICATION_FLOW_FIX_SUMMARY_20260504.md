# 闲鱼账号验证 / Cookie / 保活链路修复总结（更新至 2026-05-15）

## 当前结论

截至 **2026-05-15**，这条链路现在要分两层看：

1. **浏览器运行时已经统一**
   - 仓库里的活跃浏览器链路已经统一走 `CloakBrowser` provider。
   - 启动、自检、Docker 构建、README 安装提示都统一为：
     - `python -m cloakbrowser install`
   - 旧浏览器直连和历史废弃入口已经从活跃路径里收口，`utils/slider_patch.py`、`utils/refresh_util.py` 已删除。

2. **账号密码登录前半段不是当前主要矛盾**
   - 登录页打开、账号密码提交、滑块接管、验证码截图更新，这一段逻辑已经打通。
   - 真正需要盯的，仍然是 **滑块之后的账号风控承接**，包括：
     - 二维码验证
     - 人脸验证
     - `token_refresh` 命中的风控恢复

3. **`token_refresh` 失败不等于“浏览器又坏了”**
   - 现在更常见的情况是：账号进入风险验证页，或者保活链路命中了处罚页/验证页。
   - 重点应该看风控日志、验证截图刷新情况、会话状态，而不是一上来就甩锅给滑块。

一句话说透：

> 现在主要问题不是“老浏览器被识别导致链路根本跑不起来”，而是“账号在滑块后还要不要继续做人工验证，以及保活恢复能不能正确承接这个状态”。

---

## 2026-05-14 这轮修复/调整（重点注意）

这一轮主要是在“**无头 + 指纹稳定 + 登录后 Cookie 承接/复用**”这几个点把坑补齐，核心目标：

1) `account_id=1` 这类“已登录账号”在后续流程里不应该动不动就要求重新登录  
2) 全链路必须可在 **Docker 无头**稳定跑起来（不能依赖有头窗口）  
3) 人脸/滑块的交互不要出现“抽搐”（双重拟人化叠加）  
4) 密码登录完成后，Cookie 能落库并被后续 WS/保活/刷新链路正确接管

### A. 指纹浏览器会话不稳定（根因：fingerprint seed 每次随机）

- **现象**：即使复用 `browser_data/user_<account_id>`，同一账号后续流程仍然可能被要求重新登录。
- **根因**：`CloakBrowser` 默认每次 launch 会随机生成 `--fingerprint=<seed>`，导致“设备画像”在变。
- **修复**：
  - `utils/xianyu_slider_stealth.py`：当 provider 接管浏览器身份时，为账号生成稳定 `--fingerprint=`，并支持环境变量覆盖：
    - `XY_CLOAKBROWSER_FINGERPRINT=<seed>`
  - `utils/account_browser_runtime.py`：managed runtime 启动时同样补齐稳定 `--fingerprint=`（覆盖所有 runtime 入口，避免漏网）。

### B. “扫完人脸后滑块抽搐”（根因：provider humanize + 自研轨迹叠加）

- **现象**：滑块表现为往右拉又往左、一顿一顿的“抽搐”。
- **根因**：CloakBrowser humanize 会替换 `page.mouse.*` 为拟人化实现；而我们自研轨迹本身也包含超调/回退/微调。两套叠加就会抖。
- **修复**：
  - 默认路径：在 `simulate_slide()` 内检测到 `page._original` 时，改用 `page._original.mouse` 执行轨迹，避免双重拟人化。
  - 可选开关：若你想“完全交给指纹浏览器自带的滑块处理”，可以设置：
    - `XY_SLIDER_USE_PROVIDER_HUMANIZE_DRAG=1`
    - 开启后走 provider 的简单拖拽，不再生成自研物理轨迹（避免叠加）。

### C. 新账号首次密码登录成功后落库失败（KeyError / “账号不存在”）

- **现象**：第一次密码登录拿到 Cookie 后，保存流程报错（典型是 `账号不存在: 1` / KeyError）。
- **根因**：新账号还没插入 cookies 记录时，就先做 unb 绑定校验，导致绑定逻辑内部拿不到账号 cookie 行。
- **修复**：`reply_server.py` 调整顺序：
  - **老账号**：仍然先做 unb 冲突校验，再更新 cookie（避免覆盖到别的账号）
  - **新账号**：先 `update_cookie_account_info(INSERT)` 落库，再做 unb 绑定

### D. 密码登录 runtime 未释放导致“profile 被占用”，后续稳定化流程失败

- **现象**：密码登录完成后进入 token 预检/稳定化降级流程时，报 `profile_dir 已被其他 runtime 持有`。
- **根因**：密码登录的 managed runtime 独占锁未释放，稳定化又要复用同一 profile。
- **修复**：`reply_server.py` 在拿到 `cookies_dict` 后立即释放 `runtime_lease`，再进入预检/稳定化流程。

### E. DevToolsActivePort 残留导致 connect_over_cdp() 连到旧端口（ECONNREFUSED）

- **现象**：偶发 `ECONNREFUSED`，看起来像 CDP 连接不上。
- **根因**：上一次异常退出后，`browser_data/.../DevToolsActivePort` 残留，里面端口已无效。
- **修复**：`utils/browser_provider.py` 在启动 managed runtime 前 best-effort 删除 `DevToolsActivePort`。

### F. 无头模式下仍弹“有头窗口”（根因：managed runtime 没有传 headless CLI）

- **现象**：你肉眼看到像“起了两个有头浏览器”，扫完了窗口还会闪/弹。
- **根因**：managed runtime 是 subprocess 直接起 Chromium 二进制；`headless=True` 如果不转成 Chromium CLI 参数，并不会真的无头。
- **修复**：`utils/browser_provider.py` 在 `headless=True` 时强制追加：
  - `--headless=new`

### G. 旧日志/轨迹等历史数据的清理口径

本轮测试前清理了以下运行期数据目录（用于排除历史干扰，不建议在生产频繁乱删 profile）：
- `logs/`
- `trajectory_history/`
- `__pycache__/`
- `static/uploads/images/`
- `browser_data/user_1`（账号 1 的 profile 目录，删除后会自动重建）

### H. 风控日志 risk_control_logs 遗留结构自动修复（cookie_id -> account_id）

- **背景**：历史版本里 `risk_control_logs` 曾用 `cookie_id` 之类字段去关联账号；新版本统一按 `account_id` 作用域查询/写入。
- **风险**：如果用户数据库里还残留旧结构，会导致风控日志查询、会话排查口径混乱（看起来像“没记录/记录丢了”）。
- **修复**：`db_manager.py` 在数据库迁移时检测到遗留字段会自动重建表结构为 `account_id`，并迁移可识别记录。
- **验证**：新增单测覆盖该迁移逻辑（`tests/test_db_manager_account_id_relations.py`）。

### I. Windows 调试：人脸/扫码截图快速打开

- `debug_manual_password_login.py` 在拿到验证截图路径后（Windows）会尝试 `os.startfile()` 自动打开图片，便于人工处理验证。

### J. 登录/导入入口统一按 account_id 复用账号画像（避免“同账号已登录仍要重登”）

- **背景**：`/password-login`、`/manual-cookie-import` 入口在 API 层创建 `XianyuSliderStealth` 时，若未显式开启账号持久化画像，会导致 managed runtime 走 `new_context()`（非持久化上下文），表现为：
  - 同 `account_id` 明明已登录，后续仍可能被要求重新登录；
  - 有头调试时更容易出现“看起来像两个窗口/两次启动”的观感（一个是持久化 profile 的宿主窗口，一个是新建的匿名 context）。
- **调整**：`reply_server.py` 在这两个入口创建 `XianyuSliderStealth` 时统一强制：
  - `use_account_persistent_profile=True`
  - 从而保证所有登录/导入/验证链路都按 `browser_data/user_<account_id>` 这套账号画像复用。

### K. 减少“密码登录后二次拉起浏览器”的概率（优先复用同一次 runtime）

- **背景**：旧流程为了让 token 预检/稳定化链路重新申请 runtime，会在拿到 cookies 后立刻释放 password_login 的 sync runtime，表现为“看起来又启动了一次浏览器/又开了一个窗口”。  
- **调整**：
  - `reply_server.py::_stabilize_password_login_cookies_after_login()` 新增“HTTP Token 预检 + 原 runtime 内稳定化”优先级：
    - 先用 `probe_cookie_verification_from_cookie()` 做 token 探测（纯 HTTP，无浏览器）。
    - 若未通过，尝试直接调用当前 `slider_instance._stabilize_logged_in_context_cookies()` 在同一 managed runtime 内访问 `https://www.goofish.com/im` 等动作，补齐延迟下发 Cookie 后再探测。
  - `reply_server.py` 的 password-login 正常模式不再提前释放 runtime，而是尽量复用到稳定化完成（刷新模式仍保持提前释放以支持 async 预检/恢复）。
  - 兜底策略仍保留：若复用失败，则释放/失效当前 runtime 后再走旧的 async 稳定化（仍可能再次申请 runtime）。

--- 

## 2026-05-15 这轮修复/调整（二维码扫码登录“扫了没反应”/服务卡死）

### A. 现象

- 扫码后前端一直停在 `waiting` / `confirmed`，看起来“扫了没反应”。  
- 极端情况下，`/qr-login/check/{session_id}` 甚至 `/health` 会超时无响应，但进程仍在 listen（典型就是 API 线程被卡住了）。

### B. 根因（组合拳）

1) **managed runtime 关闭阶段可能卡死**  
`await runtime.browser.close()` / `await playwright.stop()` 在 CDP attach 场景下有概率卡住，导致 runtime invalidation/切换流程拖住后续链路。

2) **跨线程调用 CookieManager 更新，可能阻塞 API event loop**  
扫码 cookie 交接时，如果在 API server 的 event loop 线程里直接调用 `cookie_manager.manager.update_cookie()`，内部跨线程 `future.result()` 在遇到 runtime 关闭卡住时会被放大成“接口不返回”。

### C. 修复点（本次落地）

- `utils/browser_provider.py`
  - `close_managed_browser_runtime_async()`：对 `browser.close()` / `playwright.stop()` 增加 `asyncio.wait_for(..., timeout=close_timeout)`，避免无限挂死。
  - `launch_managed_browser_runtime*()`：启动前 best-effort 清理陈旧锁文件：`DevToolsActivePort`、`lockfile`、`SingletonLock/Cookie/Socket`，降低“残留导致启动失败/端口陈旧”的概率。
- `reply_server.py`
  - `process_qr_login_cookies()`：把 CookieManager 的 add/update 强制投递到 `cookie_manager.manager.loop` 内执行，避免 API 线程被跨线程阻塞拖死。
  - 任务切换采用**软超时**：`_run_live_instance_on_manager_loop(..., timeout=15, cancel_on_timeout=False)`，超时只是不再等待（避免“扫了没反应”），但不会取消 manager.loop 内部的切换协程，后台会继续跑完。
  - 扫码链路不再因为 `task_restarted=False` 就把会话标记为失败；真实 Cookie 已落库时，不回滚/不删除，最多给前端一个 warning。
- `XianyuAutoAsync.py`
  - `_verify_cookie_validity()`：补齐 `tempfile` / `SecureConfirm` 的本地导入，避免 cookie 健康探测误报（之前会直接 NameError）。

### D. 验证方式（人工/接口）

1) 走一遍扫码登录链路：`POST /qr-login/generate` -> 轮询 `GET /qr-login/check/{session_id}`  
2) 预期状态序列：`waiting -> scanned -> confirmed -> success`  
3) 在 `confirmed` 阶段，`/health` 仍应保持可用（不再出现“服务 listen 但接口不回”的假死现象）。

## 这轮迁移收口了什么

### 1. 运行时统一到 CloakBrowser

- `requirements.txt`：浏览器依赖改成 `cloakbrowser`
- `Start.py`：启动自检改成检查 `CloakBrowser` provider 和 runtime
- `Dockerfile` / `Dockerfile-cn`：构建阶段改为执行 `python -m cloakbrowser install`
- `docker-compose.yml` / `docker-compose-cn.yml`：保留代理构建参数透传
- `README.md`：安装和访问口径同步到当前配置

### 2. 活跃浏览器链路已统一

以下活跃路径已接到统一 provider：

- 主登录 / 滑块链路
- `token_refresh` 浏览器恢复链路
- 搜索旁路
- 订单详情抓取
- 二维码相关链路
- 远程验证码控制链路

### 3. 删除旧尸体

已删除：

- `utils/slider_patch.py`
- `utils/refresh_util.py`

这些文件继续留着只有一个作用：把人看晕，顺便误导后续排查方向。

---

## 当前排查重点

### A. 看 `token_refresh` 风控恢复有没有接住

重点关注：

- 是否命中 `token_refresh` 场景的风控日志
- 验证截图是否持续刷新
- 页面是否已经进入二维码 / 人脸验证
- 恢复链路是否错误地把处罚页当成普通滑块页

### B. 看账号是不是已经进入人工验证状态

如果日志和截图已经明确进入：

- 二维码验证
- 人脸验证

那这不是“账号密码登录没通”，而是 **账号被要求继续做人机验证**。

### C. 不要乱删账号级浏览器状态

下面这个目录属于运行期关键状态：

- `browser_data/user_<account_id>`

这个目录里保存了账号级 profile、站点状态和恢复链路需要的上下文。  
别手一抖清了，清完再说“怎么又得重新验证”，那就是自己给自己找活。

---

## 本地验证建议

### 1. 先确认 runtime

```powershell
.\.venv\Scripts\python.exe -m cloakbrowser info
.\.venv\Scripts\python.exe -m cloakbrowser install
```

### 2. 本地直接启动

```powershell
.\.venv\Scripts\python.exe Start.py
```

默认地址：

- `http://localhost:8090`
- `http://localhost:8090/docs`
- `http://localhost:8090/health`

### 3. 本地调试账号密码链路

优先先做本地非 Docker 验证，再决定是否同步到 Linux。

建议检查：

- 登录页是否正常打开
- 提交账号密码后是否正确进入滑块
- 滑块后是否进入二维码 / 人脸验证
- 验证截图是否持续刷新
- 风控日志是否和页面状态一致

---

## Linux / Docker 侧注意事项

如果要在 Linux 上重建：

```bash
export HTTPS_PROXY=http://192.168.31.188:10809
docker compose -f docker-compose-cn.yml up -d --build
```

如果下载依赖走本地代理，也可以使用：

- `http://127.0.0.1:1081`

当前仓库已经把构建参数透传到 Docker build，不需要再手工改 Dockerfile 逻辑。

---

## 现在的判断标准

### 不是问题本身的现象

以下情况，**不能直接判定为“浏览器又崩了”**：

- 滑块后进入二维码验证
- 滑块后进入人脸验证
- `token_refresh` 命中风险恢复
- 账号需要人工继续处理

### 真正值得报警的信号

以下情况才说明链路本身还有问题：

- 登录页打不开或表单异常
- 滑块根本接管不上
- 验证截图不刷新
- 风控日志和实际页面状态明显不一致
- `token_refresh` 已进入验证页，但恢复链路没有正确记录和承接

---

## 当前收口口径

1. **浏览器运行时统一为 CloakBrowser**
2. **不再保留旧浏览器 fallback 作为正式方案**
3. **本地先验证，过了再同步 Linux**
4. **后续重点排查账号风控承接，不再重复纠缠旧浏览器栈**
