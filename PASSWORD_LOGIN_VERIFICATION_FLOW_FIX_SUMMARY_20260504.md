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

## 2026-05-15 本轮完整回归（清数据 -> 人脸 -> 滑块 -> Cookie / WS 接管）

本轮按用户要求做了一次更严格的真实回归，特点不是“沿用旧数据碰碰运气”，而是先把 `account_id=1` 的历史状态清干净，再走一遍完整链路：

- 清理范围：
  - `browser_data/user_1`
  - `data/xianyu_data.db` 中 `cookies.id='1'`
  - `data/xianyu_data.db` 中 `risk_control_logs.account_id='1'`
  - `logs/`
  - `static/uploads/images/`
- 登录参数：
  - `account_id=1`
  - 账号：`15614318625`
  - 模式：`show_browser=false`
- 本轮会话：
  - `session_id=ZgcnSXqyhqEfRuu0aLfp3A`

### 本轮关键结果

1. **无头账密登录 + 人脸验证 + 后续滑块全部跑通**
   - 先触发人脸验证；
   - 用户扫脸后，又触发了一次滑块；
   - 滑块通过后，页面重新确认登录成功。

2. **前端验证状态误判已修正**
   - 之前存在“用户还没扫脸，接口状态却先跳成 `验证已提交，正在等待登录完成`”的问题；
   - 根因不是用户操作错了，而是：
     - 验证页等待期间 iframe 会反复刷新；
     - 某些轮次新探测没拿到新的截图/链接；
     - 旧逻辑把“当前没素材”误当成“用户已经提交验证”。
   - 本次已在 `reply_server.py` 增加验证素材保留逻辑：
     - 同一验证类型下，若新一轮探测没有拿到新的截图/链接；
     - 会继续沿用会话里上一份仍然存在的有效截图 / 验证链接；
     - 不再错误降级成“已提交等待完成”。

3. **密码登录成功后 Cookie 已正确落库并由正式实例接管**
   - `password-login/check` 最终返回：
     - `status = success`
     - `cookie_count = 21`
     - `token_prewarmed = false`
     - `real_cookie_refreshed = false`
   - 注意这里的 `token_prewarmed=false` 是**语义修正后的正确结果**：
     - 只表示“当前不是浏览器内提前完成正式 token 初始化”；
     - 并不表示链路失败；
     - 正式 token 由后台实例初始化。

4. **正式实例后续链路已确认正常**
   - `/accounts/1/runtime-status` 实测返回：
     - `instance_exists = true`
     - `running = true`
     - `connection_state = connected`
     - `ws_ready = true`
     - `session_ready = true`
     - `has_current_token = true`
     - `token_refresh_status = success`
     - `message_stream_status = healthy`

### 本轮日志结论

- `17:16:25`：接口返回 `需要人脸验证，请查看验证截图`
- `17:17:51`：人脸后续补发的滑块处理成功
- `17:17:56`：页面元素确认登录成功
- `17:18:22`：验证完成后获取到 `25` 个浏览器侧 Cookie 字段
- `17:18:23`：密码登录成功回写到服务侧，保护性合并后落库 `21` 个字段
- `17:18:23`：`密码登录后的Token预检(HTTP)` 通过
- `17:18:27`：正式实例 `refresh_token` 成功
- `17:18:28`：WebSocket 初始化完成并进入 `connected`
- `17:18:30`：开始正常收包、心跳和业务消息处理

### 本轮额外确认到的点

#### A. “接管后 profile 自抢锁”这轮没有再复发

本轮没有再出现之前那种：

- 登录阶段 runtime 还占着 `browser_data/user_1`
- 正式实例或浏览器稳定化又去抢同一个 profile
- 最终报 `profile_dir 已被其他 runtime 持有`

说明本轮涉及的两处修正已经生效：

- 密码登录成功后，普通登录链路会在 handoff 前释放登录阶段 runtime；
- 浏览器稳定化 / Cookie 恢复 runtime 在释放后会立即失效缓存实例，避免 runtime manager 继续短时占用 profile。

#### B. `account_id` 级画像复用已在真实链路里生效

这轮是清掉 `browser_data/user_1` 后重新拉起的首轮验证；后续整条链路仍然始终围绕：

- `account_id=1`
- `browser_data/user_1`

进行，没有回退成匿名临时上下文，也没有混到别的账号目录。

#### C. 仍有一个“不影响主链路，但值得继续收”的点

浏览器侧最终仍提示缺少两个保护字段：

- `cna`
- `havana_lgc2_77`

但这次已经验证：

- 浏览器业务预热能拿到 `login.token` / `loginuser.get` 的 `200` 成功响应；
- 正式实例能正常 `refresh_token`；
- WebSocket / 心跳 / session keepalive 全部正常。

所以当前它**不会阻塞主链路**，但后续仍建议继续优化“最终持久化 Cookie 视图”的完整性，减少后续恢复链路的补票压力。

#### D. “两个 Start.py” 不是两个平级服务乱跑

本轮额外查到的实际情况是：

- 有父进程 `Start.py`
- 以及一个子进程 `Start.py`
- 真正监听 `8090` 的是子进程

所以这更像是启动模型里的父子进程结构，而不是两个独立服务实例都在抢同一个业务端口。它暂时不影响本轮回归结论，但后续上 Docker 前仍建议再收口一次进程模型。

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

## 2026-05-15 本次二维码扫码登录实测（`account_id=1`）

这轮不是纸上谈兵，是真扫了一轮码，后续链路也盯到位了。

- 账号：`15614318625`
- `account_id`：`1`
- 二维码会话：`35aa6770-5126-40da-a2e0-ebe87dc10af7`
- 模式：无头 + `account_id` 持久化画像 `browser_data/user_1`

### 本轮确认结果

1. **扫码登录本身已成功**
   - `17:47:59`：日志确认 `扫码登录成功（来源: api）`
   - 扫码 Cookie 与 `unb=2095002164` 已拿到

2. **真实 Cookie 已补齐并落库**
   - `17:48:22`：开始复用 `browser_data/user_1` 获取真实 Cookie
   - `17:48:26`：真实 Cookie 获取成功并保存到数据库

3. **正式账号实例最终已接管成功**
   - `17:53:17`：旧实例退出后，新实例重新启动
   - `17:53:20`：`refresh_token` 成功
   - `17:53:21`：`WebSocket初始化完成`
   - `17:53:21`：连接状态进入 `connecting -> connected`

4. **后续 Cookie / 业务链路也正常**
   - `17:53:29`：浏览器 Cookie 刷新完成
   - `17:53:31`：Cookie 有效性验证通过
   - 图片上传验证也通过，说明不是“只登录表面成功”

### 这轮顺手修掉/确认的坑

- **同一 `account_id` 多个二维码 session 混线**
  - 现在生成新二维码前，会先失效同账号旧 session
  - 避免“前端盯新二维码，你手机扫旧二维码，结果看起来像没反应”

- **二维码监控脏日志**
  - 旧 session 被替换后，不再误记成“二维码超时过期”
  - 现在会明确记录：`会话已被替换/清理，停止轮询`

- **扫码后 handoff 假超时**
  - 之前账号任务切换软超时是 `15s`
  - 这次真实日志里，切换完成时间已经贴着这个阈值跑
  - 现已放宽到 `25s`，减少“其实快成功了，前端先看到处理中/误告警”的傻逼场面

- **接管成功后立刻又触发一次浏览器 Cookie refresh**
  - 根因不是“又有新风控”，而是新实例启动后 `last_cookie_refresh_time=0`
  - `cookie_refresh_loop()` 一启动就会判断“已经超出 3 小时间隔”，于是立刻拉起一次浏览器刷新
  - 现已在正式实例启动 Cookie 刷新任务前，先初始化一次刷新基线
  - 效果：新实例接管成功后，不会马上再来一轮冗余浏览器刷新

### 当前口径

- 这轮 **二维码扫码登录主链路已经跑通**
- `account_id=1` 确实全程复用了同一份持久化画像
- 现在剩下的优化重点不是“扫不通”，而是：
  - 减少接管后的冗余浏览器刷新
  - 继续收紧风控恢复阶段的时序和提示文案

---

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
