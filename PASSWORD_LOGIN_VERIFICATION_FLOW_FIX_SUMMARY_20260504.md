# 闲鱼账号验证 / Cookie / 保活链路修复总结（更新至 2026-05-15）

## 当前结论

截至 **2026-05-15**，这条链路现在要分两层看：

1. **浏览器运行时已经统一**
   - 项目活跃浏览器链路已经统一到 `CloakBrowser` provider。
   - 安装 / Docker 构建 / 启动自检都统一为：
     - `python -m cloakbrowser install`

2. **账号密码登录前半段已经打通**
   - 登录页打开、账号密码提交、滑块接管、验证码截图更新，这一段已经可用。
   - 当前真正要盯的是**滑块之后的账号风控承接**，包括：
     - 二维码验证
     - 人脸验证
     - `token_refresh` 命中的风控恢复

3. **`token_refresh` 失败不等于“浏览器又坏了”**
   - 更常见的是账号进入风险验证页，或者保活链路命中了处罚页 / 验证页。
   - 重点要看风控日志、验证截图、会话状态，不要一上来就把锅甩给滑块。

一句话说透：

> 现在主要问题不再是“浏览器链路根本跑不起来”，而是“滑块之后账号是否还要继续做人机验证，以及保活恢复能不能正确承接这个状态”。

---

## 2026-05-15 本次新增验证结果（无头账号密码登录已跑通）

本次针对 `account_id=1` 做了一轮**无头**账号密码登录实测：

- 账号：`15614318625`
- `account_id`：`1`
- 模式：`show_browser=false`
- 会话：`DzF4yAB8Zk_jnvP59N1GLg`

### 本次结果

1. **未触发人脸 / 扫码**
   - 本轮不需要人工参与。

2. **账号密码登录成功**
   - API 最终返回：`success`
   - 返回字段：
     - `cookie_count = 26`
     - `token_prewarmed = true`
     - `real_cookie_refreshed = false`

3. **Cookie 已正确落库并被后续流程接管**
   - 登录成功后拿到 26 个 Cookie 字段；
   - `Token预检(HTTP)` 直接通过；
   - 后续正式实例继续完成 token 初始化；
   - WebSocket / 后台任务已正常接管。

### 这次能成功的关键原因

- `reply_server.py` 中**普通密码登录**的后置交接链路已修正：
  - 不再把“登录成功后拿到的新 Cookie”立刻判成必须走“二次拉起同账号 persistent profile 做稳定化”；
  - 优先走当前 Cookie 的 `Token预检(HTTP)`；
  - 避免了之前那种：
    - 前半段其实已经登录成功；
    - 后面又去抢同一个 `browser_data/user_<account_id>`；
    - 最终因为 profile / runtime 冲突把本来成功的链路判失败。

### 本次日志结论

- `15:02:20`：账号密码登录成功，获取到 `26` 个 Cookie 字段
- `15:02:20`：`密码登录后的Token预检(HTTP)通过`
- `15:02:25`：正式实例后续 `token refresh` 成功
- `15:02:26`：实例初始化完成，WebSocket / 后台任务正常启动

### 仍待继续优化的点

- 虽然本次主链路已经成功，但正式实例接管后，`cookie_refresh_loop` 仍会很快再触发一次浏览器 Cookie 刷新；
- 该旁路日志里仍可能出现：
  - `CloakBrowser process exited before DevToolsActivePort was ready`
- **当前它不会影响本次“登录成功 -> Cookie落库 -> Token接管 -> 实例启动”主链路**；
- 后续仍建议继续收紧这段“接管后立刻再刷一次浏览器”的策略，减少无意义的二次浏览器启动。

---

## 2026-05-14 这轮修复 / 调整（重点）

这一轮主要是在 **无头 + 指纹稳定 + 登录后 Cookie 承接 / 复用** 这几个点补坑，核心目标：

1. `account_id=1` 这类“已登录账号”在后续流程里不应该动不动就要求重新登录
2. 全链路必须能在 **Docker 无头** 模式稳定运行
3. 人脸 / 滑块交互不能出现“双重拟人化导致抽搐”
4. 密码登录完成后，Cookie 要能落库并被后续 WS / 保活 / 刷新链路正确接管

### A. 指纹浏览器会话不稳定（根因：fingerprint seed 每次随机）

- **现象**
  - 即使复用 `browser_data/user_<account_id>`，同一账号后续流程仍可能被要求重新登录。
- **根因**
  - `CloakBrowser` 默认每次 launch 可能生成新的 `--fingerprint=<seed>`。
- **修复**
  - `utils/xianyu_slider_stealth.py`
  - `utils/account_browser_runtime.py`
  - 统一按 `account_id` 注入稳定 `--fingerprint=...`
  - 支持环境变量覆盖：
    - `XY_CLOAKBROWSER_FINGERPRINT=<seed>`

### B. “扫完人脸后滑块抽搐”（根因：provider humanize + 自研轨迹叠加）

- **现象**
  - 滑块表现为往右拉又往左、一顿一顿的。
- **根因**
  - `CloakBrowser` 会 humanize `page.mouse.*`
  - 我们自己的轨迹也带超调 / 回退 / 微调
  - 两层叠加就容易抖
- **修复**
  - `simulate_slide()` 在检测到 `page._original` 时，优先使用原始鼠标接口执行轨迹；
  - 若 `page._original` 暴露的是 `mouse_move / mouse_down / mouse_up`，则做适配；
  - 避免 provider humanize 和自研轨迹双重生效。

### C. 新账号首次密码登录成功后落库失败（KeyError / “账号不存在”）

- **现象**
  - 第一次密码登录拿到 Cookie 后，保存流程报错。
- **根因**
  - 新账号还没插入 cookies 记录时，就先做 `unb` 绑定校验。
- **修复**
  - `reply_server.py`
  - 老账号：先做 `unb` 冲突校验，再更新 Cookie
  - 新账号：先落库，再做 `unb` 绑定

### D. 密码登录 runtime 未释放导致 “profile 被占用”

- **现象**
  - 密码登录完成后进入 token 预检 / 稳定化流程时，报 `profile_dir 已被其他 runtime 持有`
- **根因**
  - managed runtime 独占锁未释放，又想复用同一 profile
- **修复**
  - `reply_server.py`
  - 调整 runtime 释放和交接时机

### E. `DevToolsActivePort` 残留导致 `connect_over_cdp()` 连到旧端口

- **现象**
  - 偶发 `ECONNREFUSED`
- **根因**
  - 上次异常退出后，`DevToolsActivePort` 文件残留
- **修复**
  - `utils/browser_provider.py`
  - 启动 managed runtime 前 best-effort 清理残留端口文件

### F. 无头模式下仍开“有头窗口”

- **现象**
  - 看起来像“起了两个浏览器窗口”
- **根因**
  - managed runtime 是 subprocess 直接起 Chromium，`headless=True` 不转成 CLI 参数就不是真无头
- **修复**
  - `utils/browser_provider.py`
  - 强制补 `--headless=new`

### F2. Docker 部署硬约束：服务入口统一强制 headless

- 新增环境变量：
  - `XY_FORCE_HEADLESS`
- 开启后：
  - `/password-login`
  - `/manual-cookie-import`
  - `/qr-login`
  - 都统一按无头模式运行

### G. 旧日志 / 轨迹 / 历史运行数据清理口径

测试前清理过这些目录以排除历史干扰：

- `logs/`
- `trajectory_history/`
- `__pycache__/`
- `static/uploads/images/`
- `browser_data/user_1`

### H. `risk_control_logs` 遗留结构修复（`cookie_id -> account_id`）

- **背景**
  - 历史版本里风控日志曾使用 `cookie_id`
- **修复**
  - `db_manager.py` 自动迁移到 `account_id`
- **验证**
  - 对应单测已补齐

### I. Windows 调试：人脸 / 扫码截图自动打开

- `debug_manual_password_login.py`
  - 拿到验证截图后，在 Windows 下尝试 `os.startfile()` 自动打开图片

### J. 登录 / 导入入口统一按 `account_id` 复用账号画像

- **修复**
  - `reply_server.py`
  - `password-login` / `manual-cookie-import` 创建 `XianyuSliderStealth` 时统一：
    - `use_account_persistent_profile=True`

### K. 减少“密码登录后二次拉起浏览器”的概率

- **修复方向**
  - `reply_server.py::_stabilize_password_login_cookies_after_login()`
  - 优先走：
    1. `Token预检(HTTP)`
    2. 当前 managed runtime 内 Cookie 稳定化
    3. 只有必要时再走旧兜底

---

## 2026-05-15 这轮修复 / 调整（二次扫码登录“扫了没反应” / 服务假死）

### A. 现象

- 扫码后前端一直停在 `waiting` / `confirmed`
- 极端情况下 `/qr-login/check/{session_id}` 甚至 `/health` 都不返回

### B. 根因（组合拳）

1. managed runtime 关闭阶段可能卡死
2. 跨线程调用 `CookieManager` 更新时，可能阻塞 API event loop

### C. 已做修复

- `utils/browser_provider.py`
  - 对 `browser.close()` / `playwright.stop()` 增加超时保护
  - 启动前清理残留锁文件
- `reply_server.py`
  - CookieManager 的 add/update 投递到 manager.loop
  - 二维码链路切换采用软超时，不再让前端一直傻等
- `XianyuAutoAsync.py`
  - 补齐本地导入，避免 cookie 健康检查 NameError

---

## 这轮迁移收口了什么

### 1. 运行时统一到 `CloakBrowser`

- `requirements.txt`
- `Start.py`
- `Dockerfile`
- `Dockerfile-cn`
- `docker-compose.yml`
- `docker-compose-cn.yml`
- `README.md`

### 2. 活跃浏览器链路已统一

以下活跃路径已接到统一 provider：

- 主登录 / 滑块链路
- `token_refresh` 浏览器恢复链路
- 搜索侧链路
- 订单详情抓取
- 二维码相关链路
- 远程验证码控制链路

### 3. 删除旧实现

已删除：

- `utils/slider_patch.py`
- `utils/refresh_util.py`

---

## 当前排查重点

### A. 看 `token_refresh` 风控恢复有没有接住

重点关注：

- 是否命中 `token_refresh` 场景的风控日志
- 验证截图是否持续刷新
- 页面是否已经进入二维码 / 人脸验证
- 恢复链路是否误把处罚页当普通滑块页

### B. 看账号是不是已经进入人工验证状态

如果日志和截图已经明确进入：

- 二维码验证
- 人脸验证

那这不是“账号密码登录没通”，而是 **账号被要求继续做人机验证**。

### C. 不要乱删账号级浏览器状态

关键目录：

- `browser_data/user_<account_id>`

这里保存的是账号级 profile 和后续恢复链路要用的上下文。别手一抖删了，再问“怎么又要重新验证”，那就是自己给自己找活。

---

## 本地验证建议

### 1. 先确认 runtime

```powershell
.\.venv\Scripts\python.exe -m cloakbrowser info
.\.venv\Scripts\python.exe -m cloakbrowser install
```

### 2. 本地启动

```powershell
.\.venv\Scripts\python.exe Start.py
```

默认地址：

- `http://localhost:8090`
- `http://localhost:8090/docs`
- `http://localhost:8090/health`

### 3. 本地优先验证账号密码链路

重点检查：

- 登录页是否正常打开
- 提交账密后是否正确进入滑块
- 滑块后是否进入二维码 / 人脸验证
- 验证截图是否持续刷新
- 风控日志是否和页面状态一致

---

## Linux / Docker 侧注意事项

```bash
export HTTPS_PROXY=http://192.168.31.188:10809
docker compose -f docker-compose-cn.yml up -d --build
```

如需本地代理，也可使用：

- `http://127.0.0.1:1081`

---

## 现在的判断标准

### 不算浏览器链路本身有问题的现象

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

1. **浏览器运行时统一到 `CloakBrowser`**
2. **所有登录 / 导入 / 恢复流程统一按 `account_id -> browser_data/user_<account_id>` 复用**
3. **无头模式作为 Docker 部署默认前提**
4. **后续重点继续收紧风控承接与 Cookie / Token 交接，不再回退旧浏览器栈**
