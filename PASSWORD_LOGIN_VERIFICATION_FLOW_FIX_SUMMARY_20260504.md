# 闲鱼账号验证 / Cookie / 保活链路修复总结（更新至 2026-05-17）

## 当前结论

截至 **2026-05-17**，这条链路现在要分两层看：

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

## 2026-05-17 本次代码级收口（指纹 / 滑块 / 账号级画像继续收紧）

这轮重点不是重新吹“能登录了”，而是把 **CloakBrowser 自己负责的那层能力** 和 **项目侧自己乱加的一层** 往开掰，省得又把同一个 `account_id` 弄成两套画像、两套路数。

### A. 滑块优先走 CloakBrowser 自己的 humanize 拖拽

涉及文件：

- `utils/xianyu_slider_stealth.py`
- `tests/test_slider_verification_guards.py`

本次调整：

1. 新增 `_should_use_provider_humanize_drag()`
   - 只要当前 automation backend 是 `cloakbrowser`
   - 默认就走 provider 自己的 humanize drag
   - 只有显式设置 `XY_SLIDER_USE_PROVIDER_HUMANIZE_DRAG=0/false/no/off` 才回退到项目侧自研轨迹

2. `simulate_slide()` / 轨迹生成分支同步收口
   - 默认直接走 `_simulate_slide_with_provider_humanize()`
   - 不再要求额外开环境变量才启用 provider 拖拽

这样做的意义很直接：**昨天已经验证能过的滑块，今天别又让项目侧物理轨迹上去瞎搅和。**

### B. CloakBrowser 接管画像时，项目侧不再二次塞 locale / timezone / viewport

涉及文件：

- `utils/xianyu_slider_stealth.py`
- `utils/qr_login.py`
- `tests/test_slider_verification_guards.py`
- `tests/test_qr_login_status_flow.py`

本次调整：

1. `XianyuSliderStealth._build_browser_context_options()`
   - 对 `cloakbrowser` 直接返回空 dict
   - 不再把项目侧随机 `locale/timezone/viewport/color_scheme` 强塞回 provider

2. `XianyuSliderStealth._build_persistent_context_options()`
   - 只保留真正通用的：
     - `accept_downloads=True`
     - `ignore_https_errors=True`

3. `QRLoginManager._build_verification_context_options()`
   - 二维码验证页同样只保留通用上下文参数
   - 不再在验证 runtime 里额外覆盖身份画像

一句话：**同一个 `account_id` 的持久画像，由 CloakBrowser runtime 说了算；项目侧别再自作聪明叠一层伪指纹。**

### C. 运行时页面指标反向回填到 `browser_features`

涉及文件：

- `utils/xianyu_slider_stealth.py`

本次调整：

1. 新增 `_refresh_browser_features_from_page_metrics()`
   - 从真实页面读取：
     - `window.innerWidth`
     - `window.innerHeight`
     - `screen.width`
     - `screen.height`
     - `navigator.language`
   - 再回填到 `browser_features`

2. 在以下入口补调用：
   - 持久化 context 首次拿到 page 后
   - attach managed runtime 后
   - 浏览器恢复页 / 稳定化页打开后

目的不是“造新画像”，而是**让后续逻辑看到的页面尺寸 / 语言尽量贴近 provider 真实 runtime，而不是继续抱着项目侧旧默认值瞎算。**

### D. 账密登录页不再死等 `networkidle`

涉及文件：

- `utils/xianyu_slider_stealth.py`
- `tests/test_slider_verification_guards.py`

本次调整：

- `page.goto(login_url, wait_until='networkidle', timeout=60000)`
  改成：
  - `wait_until='domcontentloaded'`
  - 再补一个短 `page.wait_for_load_state('load', timeout=10000)`

原因很朴素：`/im` 页挂长连接，死等 `networkidle` 本来就容易超时，这种写法跟自己较劲没区别。

### E. 本轮最小验证

- `python -m py_compile utils/xianyu_slider_stealth.py utils/qr_login.py XianyuAutoAsync.py`
- `pytest tests/test_slider_verification_guards.py -q`
- `pytest tests/test_qr_login_status_flow.py -q -k "verification_context_options or launch_verification_browser_context_keeps_humanize"`
- `pytest tests/test_xianyu_token_refresh_request.py -q`

结果：

- `tests/test_slider_verification_guards.py`：通过
- `tests/test_qr_login_status_flow.py` 指纹 / context 相关新增用例：通过
- `tests/test_xianyu_token_refresh_request.py`：覆盖交接阶段 `allow_password_login_recovery` 行为，通过

### F. 交接前主动失效旧 sync runtime，减少首轮接管撞 profile

涉及文件：

- `reply_server.py`
- `XianyuAutoAsync.py`
- `tests/test_xianyu_token_refresh_request.py`

本次调整：

1. 刷新模式拿到新 Cookie 后
   - 先 `release` 当前 sync runtime
   - 再显式 `invalidate_runtime_sync(account_id, ...)`

2. 普通密码登录完成交接前
   - 同样在 handoff 前主动失效旧 sync runtime

3. 二维码真实 Cookie 刚接管时
   - `init()` 首轮 token refresh 也按 handoff recovery 对待
   - 先禁掉 password-login recovery，避免刚接管就又掉回账密恢复

一句话：**接管就是接管，旧 runtime 该让位就让位，别还趴在同一个 `browser_data/user_<account_id>` 上装死。**

### G. 重启接口不再当场复制第二个 `Start.py`

涉及文件：

- `reply_server.py`
- `tests/test_reply_server_account_scope.py`

本次调整：

1. `/api/update/restart`
   - 不再在 Windows 下直接 `CREATE_NEW_CONSOLE + Popen([python, Start.py])`
   - 改成拉起一个隐藏 helper
   - helper 等当前进程退出后，再启动新的 `Start.py`

2. 这样收掉的现象
   - 父子两个 `Start.py` 同时并存
   - 两个控制台窗口
   - 进入有头链路时，两个进程各拉一套浏览器，用户看到“双浏览器 / 双窗口”

---

## 2026-05-17 本次二维码真实回归（Cookie 稳定化收口后已跑通）

这轮不是“扫上了就算赢”，而是把二维码登录后那段最容易半截子掉地上的 Cookie 补全过程，按之前账密链路已经验证过的稳定化套路重新收口了一遍。

### A. 旧二维码链路的真实根因

之前二维码扫码成功后，`refresh_cookies_from_qr_login()` 只做了比较糙的一步：

- `goto /im`
- `reload`
- 抓一次 `context.cookies()`
- 如果缺 `_m_h5_tk / _m_h5_tk_enc` 这类关键字段，就直接判失败

这就有个很蠢但很真实的问题：**扫码 API 已经成功，浏览器上下文里也已经是登录态，但 Cookie 还没在当前页访问链路里稳定长全。**

换句话说，旧逻辑不是“没登录上”，而是**登录成功后的真实 Cookie 稳定化闭环没走完**。

### B. 本次代码级收口

涉及文件：

- `XianyuAutoAsync.py`
- `tests/test_xianyu_async_browser_runtime.py`

本次调整：

1. 在 `XianyuAutoAsync.py` 新增 async 版浏览器 Cookie 稳定化 helper
   - `_normalize_browser_cookie_items`
   - `_snapshot_browser_context_cookies_async`
   - `_run_browser_cookie_stabilization_action_async`
   - `_stabilize_browser_context_cookies_async`

2. `refresh_cookies_from_qr_login()` 改为接入同一套稳定化闭环
   - 先吃当前 `goto /im + reload` 后已经拿到的 Cookie 快照
   - 只有核心会话字段还缺时，才继续补动作：
     - `reload_current`
     - `goto_home`
     - `goto_im`
     - `fresh_tab_home`
     - `fresh_tab_im`

3. 稳定化是否继续推进，统一按 `REQUIRED_SESSION_COOKIE_FIELDS` 判断
   - 不再因为某些保护字段没齐就无脑继续折腾
   - 这样能避免二维码刚接管时又平白多开 tab、多跑一轮 runtime

一句话：**二维码链路现在不再是“扫完就抓一把 Cookie 碰运气”，而是和账密链路一样，拿浏览器上下文把真实会话字段补稳定再落库。**

### C. 真实回归结果

本轮真实扫码回归参数：

- `account_id=1`
- 二维码会话：`cec1d021-ebbb-446c-90cd-e1a03a58a6f0`
- 二维码图片：`tmp_qr_account1_latest.png`

最终结果：

1. 二维码扫码确认后，状态成功切到：
   - `status=success`
   - `handoff_status=success`
   - `real_cookie_refreshed=true`
   - `task_restarted=true`

2. 真实 Cookie 成功补全并落库
   - 成功补出 `_m_h5_tk`
   - 成功补出 `_m_h5_tk_enc`
   - 同时拿到 `mtop_partitioned_detect`

3. 账号任务接管成功
   - `instance_exists=true`
   - `running=true`
   - `connection_state=connected`
   - `ws_ready=true`
   - `session_ready=true`
   - `has_current_token=true`
   - `token_refresh_status=success`

4. 数据库状态正常
   - `cookies.id='1'`
   - `bind_status='active'`
   - `bound_unb='2095002164'`

### D. 关键日志证据

日志文件：

- `logs/xianyu_2026-05-17.log`

关键证据：

- `新增的Cookie字段 (3个): mtop_partitioned_detect, _m_h5_tk, _m_h5_tk_enc`
- `真实Cookie已成功保存到数据库`
- `已将真实cookie添加到cookie_manager: 1`
- `WebSocket连接建立成功，开始初始化...`
- `Token刷新成功`
- `新实例启动时初始化 Cookie 刷新基线，避免接管后立刻又触发一次浏览器刷新`

### E. 本轮最小验证

- `pytest tests/test_xianyu_async_browser_runtime.py -q -k "refresh_cookies_from_qr_login_uses_home_and_fresh_tab_to_fill_missing_required_fields or refresh_cookies_from_qr_login_rejects_missing_required_fields_without_persisting or refresh_cookies_via_browser_page_reuses_persistent_profile_during_qr_grace"`
- `pytest tests/test_xianyu_async_browser_runtime.py -q -k "refresh_cookies_from_qr_login or refresh_cookies_via_browser_page"`
- `pytest tests/test_qr_login_status_flow.py -q -k "verification_context_options or launch_verification_browser_context_keeps_humanize or proxy_config_uses_account_id_only"`

结果：

- `tests/test_xianyu_async_browser_runtime.py` 相关稳定化用例：通过
- `tests/test_qr_login_status_flow.py` 二维码上下文 / `account_id` 收口用例：通过

---

## 2026-05-16 本次代码级收口（CloakBrowser 升级 + 登录链路清理）

这轮不是再跑人工扫码，而是把前面已经定位出来的几个“过渡期脏点”真正收掉，避免后面又绕回旧语义。

### A. CloakBrowser 已升级到当前最新版

- Python 包：`cloakbrowser==0.3.28`
- 本机 runtime：`146.0.7680.177.4`
- 校验命令：
  - `python -m pip show cloakbrowser`
  - `python -m cloakbrowser info`

这一步的目的很直接：把本地 / Docker 后续安装都钉到同一版，别再出现“本机一个 wrapper、容器里另一个 wrapper”的鬼故事。

### B. HTTP Cookie 预检不再写死旧版 Chrome 头

涉及文件：

- `utils/xianyu_slider_stealth.py`
- `utils/qr_login.py`

本次调整：

1. 新增 `get_runtime_browser_identity()`
   - 先读 `python -m cloakbrowser info`
   - 失败再回退到 `cloakbrowser.config`
   - 动态派生：
     - `User-Agent`
     - `sec-ch-ua`
     - `sec-ch-ua-mobile`
     - `sec-ch-ua-platform`

2. `probe_cookie_verification_from_cookie()`
   - 不再写死 `Chrome/133`
   - 改为复用当前 CloakBrowser runtime 的真实版本信息

3. `generate_headers()`
   - 二维码登录 API 请求头也统一改成同一套 runtime 身份

这样做的意义是：**HTTP 预检和真实浏览器 runtime 的“浏览器身份”终于对齐了**，不会再出现一边拿旧 133 去探，一边实际浏览器已经是 146 的拧巴状态。

### C. 二维码验证代理严格按 `account_id` 走

涉及文件：

- `utils/qr_login.py`

本次调整：

1. `_resolve_existing_account_proxy_config()`
   - 只认 `session.account_id`
   - 删除按 `unb` 反推旧账号代理的 fallback

2. 同时补了一个小坑
   - 账号分支命中代理配置时，顺手回填 `session.proxy_url`
   - 避免 `session.proxy_config` 有值但 `session.proxy_url` 没同步，后面又二次解析

一句话：**扫码登录的验证浏览器，现在也和密码登录、Cookie 导入一样，彻底按 `account_id` 收口。**

### D. 清掉 `reply_server.py` 里的死代码 / 脏控制流

本次调整：

1. `_stabilize_password_login_cookies_after_login()`
   - 删除 `return` 后残留的旧：
     - `preflight_token_after_password_login()`
     - `_refresh_cookies_via_browser()`
   - 顺手修正文案，避免日志还在假装会“继续走旧 async 兜底”

2. `/qr-login/check/{session_id}`
   - 删除 confirmed 返回后那段不可达的 handoff 分支
   - 避免后续维护再被这坨死分支带歪

### E. 本次最小验证

- `python -m py_compile utils/xianyu_slider_stealth.py utils/qr_login.py reply_server.py`
- `pytest tests/test_manual_cookie_import_precheck.py -q`
- `pytest tests/test_qr_login_status_flow.py -k "profile or proxy_config_uses_account_id_only" -q`

结果：

- 语法通过
- 新增 / 相关最小测试通过

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

## 2026-05-15 本次二维码清画像重跑（`account_id=1`）补充结论

这轮按用户要求又做了一次更干净的二维码登录回归，不是复用旧画像硬蹭结果，而是先把 `account_id=1` 的浏览器画像和旧会话状态清掉，再重新扫码：

- 清理：
  - `browser_data/user_1`
  - `cookies.id='1'` 的旧 Cookie 值清空
  - 账号绑定状态回退到待重新绑定
- 重新扫码会话：
  - `session_id=77e57396-b68a-4f46-bacc-c2544c2b2099`

### 本轮结果

1. **二维码登录主链路再次跑通**
   - 前端轮询经历：
     - `waiting`
     - `scanned`
     - `confirmed`
     - `success`
   - 最终接口返回：
     - `status = success`
     - `phase = handoff_completed`
     - `handoff_status = success`

2. **真实 Cookie 成功获取并重新绑定到 `account_id=1`**
   - 本轮扫码后重新绑定：
     - `bound_unb = 2095002164`
     - `bind_status = active`
   - 数据库中账号 `1` 的 Cookie 已重新写回。

3. **正式实例接管和 WebSocket 正常**
   - `/accounts/1/runtime-status` 实测返回：
     - `instance_exists = true`
     - `running = true`
     - `connection_state = connected`
     - `ws_ready = true`
     - `session_ready = true`
     - `has_current_token = true`
     - `token_refresh_status = success`
     - `message_stream_status = healthy`

### 本轮额外确认出的修正点

#### A. 清空旧 Cookie 后，二维码主链路仍能重新建回账号级画像

这次不是拿旧浏览器数据凑合跑，而是清掉 `browser_data/user_1` 后重新扫码，最终仍然稳定落回：

- `account_id = 1`
- `browser_data/user_1`

说明“按 `account_id` 复用同一 persistent profile”这条约束在二维码登录链路里是成立的。

#### B. 服务启动期“空 Cookie 账号硬起任务”的脏逻辑已收口

这次清空 `cookies.id='1'` 后重启服务时，暴露出一个启动期脏点：

- `CookieManager` / `Start.py` 会把**空 Cookie 占位账号**也当成可运行账号；
- 然后直接尝试启动 `XianyuLive`；
- 日志里就会出现一次“缺少 `cookies_str`”的无意义报错。

本次已补上两层修正：

1. `cookie_manager.py`
   - `_load_from_db()` 现在只把**非空 Cookie**装入运行时 `manager.cookies`
   - 空 Cookie 占位账号不再被当成“可启动 runtime 的账号”

2. `Start.py`
   - 服务启动遍历账号时，再额外做一次 `blank cookie` 防呆判断
   - 即使上游将来再漏数据，也不会继续裸调 `start_runtime_task()`

### 本轮日志结论

- `21:59:25`：二维码状态进入 `scanned`
- `21:59:27`：进入 `handoff_processing`
- `21:59:37`：真实 Cookie 获取并回写数据库
- `21:59:39`：正式实例 `refresh_token` 成功
- `21:59:40`：WebSocket 初始化完成并进入 `connected`

一句话总结：

> 这轮已经验证：即使先清空 `account_id=1` 的旧画像和旧 Cookie，再从二维码登录重新走一遍，仍然可以正确重建账号级持久化画像、拿到真实 Cookie、完成实例接管，并稳定进入 WebSocket `connected`；同时启动期“空 Cookie 账号硬起任务”的脏日志也已经被收掉。

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
## 2026-05-16 本次新增验证结果（Cookie导入失效 / Token Refresh 命中滑块恢复链路已验证）

本轮针对 `account_id=1` 做了两段连续实测，目标不是只看“Cookie 能不能导进去”，而是验证：

1. **同一个 `account_id` 是否始终复用同一份 persistent profile**
2. **Cookie 导入成功后，后续如果 token refresh 突然命中滑块 / 处罚页，恢复链路是否还能走通**
3. **恢复完成后，Token / WebSocket / 消息流是否能重新回到正常状态**

### 本轮测试对象

- `account_id=1`
- 账号：`15614318625`
- 持久画像目录：`browser_data/user_1`
- 本轮使用方式：
  - 先用手动 Cookie 导入入口重新导入一遍账号 Cookie
  - 再调用手动 Token Refresh 调试入口，模拟“运行中突然命中滑块 / 处罚页”

### 第一步：手动 Cookie 导入验证通过

本轮先重新调用：

- `POST /manual-cookie-import`

导入结果：

- 返回：`success=true`
- `session_id=6iDDiMxz528SECTePoadAw`
- 轮询最终状态：
  - `status=success`
  - `account_id=1`
  - `cookie_count=23`

日志证据：

- `logs/xianyu_2026-05-16.log:2507`
  - `手动导入 Cookie 浏览器验证成功并已保存: 1, cookie_count=23`

这说明本轮不是拿旧会话硬混过去，而是 **`account_id=1` 的 Cookie 导入链路本身可用**。

### 第二步：模拟 Cookie 挂着挂着失效，Token Refresh 命中滑块

本轮调用：

- `POST /accounts/1/token-refresh`

请求体：

```json
{
  "simulate_captcha": true,
  "allow_password_login_recovery": true
}
```

返回结果关键字段：

```json
{
  "success": true,
  "result": {
    "success": true,
    "path": "simulated_captcha_password_login_recovery",
    "token_received": true,
    "slider_success": false,
    "password_login_recovered": true
  }
}
```

含义很明确：

- 本轮先模拟进入 `punish/captcha` 场景
- 滑块链路本身没有拿到成功结果：`slider_success=false`
- 但系统没有在这卡死，而是继续走了 **账号密码兜底恢复**
- 恢复后重新拿到了 Token：`token_received=true`

### 恢复链路关键日志

日志文件：

- `logs/xianyu_2026-05-16.log`

关键节点：

- `2570`
  - `开始调试模拟 Token 刷新命中滑块`
- `2603`
  - `调试模拟滑块失败，改走账号密码恢复链路`
- `2611`
  - `密码登录刷新前已主动失效旧的账号级浏览器 runtime`
- `2808`
  - `Cookie更新并重启任务成功`
- `2813`
  - `Token刷新成功`

这里最关键的一刀是：

- **账密恢复前先主动失效旧的账号级 runtime**

这样可以避免同一个 `browser_data/user_1` 在恢复链路里被前后两个 runtime 抢锁，不然又会冒出那种恶心人的 `DevToolsActivePort` / profile 占用问题。

### 恢复完成后的 runtime / WebSocket 状态

本轮恢复完成后，再查：

- `GET /accounts/1/runtime-status`

结果确认：

- `instance_exists = true`
- `running = true`
- `connection_state = connected`
- `ws_ready = true`
- `session_ready = true`
- `has_current_token = true`
- `message_stream_ready = true`
- `token_refresh_status = success`

最近时间点：

- `token_last_refreshed_at_display = 2026-05-16 01:05:43`
- `session_keepalive_at_display = 2026-05-16 01:06:00`
- `last_heartbeat_response_at_display = 2026-05-16 01:06:00`

说明这轮不是“表面返回 success，背后进程其实已经死球了”，而是 **恢复后 runtime / token / websocket 都真的重新接起来了**。

### 本轮结论

截至 **2026-05-16**，已经可以确认：

1. **Cookie 导入入口会按 `account_id=1` 复用同一份 persistent profile**
2. **后续即使模拟“Cookie 失效 + Token Refresh 命中滑块 / 处罚页”，恢复链路也能跑通**
3. **当滑块链路本身拿不到结果时，系统会自动切到账号密码兜底恢复**
4. **恢复成功后，Token / WebSocket / 消息流都能重新回到正常状态**

一句话收口：

> 这轮已经证明，`account_id=1` 在“Cookie 导入 -> 运行中失效 -> refresh 命中滑块 -> 账密兜底恢复 -> Token / WS 恢复”这条完整链路上，能始终围绕同一份 `browser_data/user_1` 持久画像闭环跑通。

### 目前仍需注意的边界

这次 `simulate_captcha=true` 仍然属于 **调试模拟处罚页 / 验证页**，不是线上真实下发的可拖动远端滑块挑战。

所以本轮已经证明的是：

- **恢复链路正确**

但还不是在证明：

- **真实远端滑块一定每次都能自动过**

这俩别混一块，不然脑袋一热又容易把“恢复链路验证”吹成“真实滑块通过率验证”，那就有点扯了。

---

## 2026-05-16 历史订单链路专项排查结论

### 结论

本轮已确认：

1. `account_id=1` 的 **runtime / token / websocket / 消息流本身正常**
2. 历史订单同步失败的根因，**不是** 指纹浏览器数据没复用，也 **不是** Cookie 丢失
3. 真正的问题是：当前账号访问
   - `https://seller.goofish.com/?site=COMMONPRO#/seller-trade/order-manage`
   时，页面会进入 **无权限页**
4. 因此后续调用
   - `mtop.taobao.idle.trade.merchant.sold.get`
   返回 `PERMISSION_EXCEPTION::无权限访问`
   是账号对该卖家工作台域无权限，不是请求参数细节问题

### 本轮真实验证

- 服务健康检查正常时，`/accounts/1/runtime-status` 显示：
  - `connection_state=connected`
  - `ws_ready=true`
  - `session_ready=true`
  - `has_current_token=true`
- 真实调用：
  - `POST /api/orders/history-sync`
  - `account_id=1`
  - 仍然失败
- 直接用当前 `account_id=1` 的 Cookie 做两类验证：
  - 纯 HTTP 请求 `mtop.taobao.idle.trade.merchant.sold.get`
  - 浏览器上下文内 `fetch(...)`
  - 结果都一致：`PERMISSION_EXCEPTION::无权限访问`

### 页面级证据

无头浏览器打开卖家工作台后，约 5 秒内可稳定观察到：

- 页面正文包含：
  - `当前账号没有访问权限`
  - `请切换账号重试，或联系对接人员确认`
- 最终 URL 会进入：
  - `#/no-permission?...`
- 页面标题会变成：
  - `禁止访问`

这说明当前账号在 **seller.goofish.com 卖家工作台** 这条链路上本身就没有权限。

### 代码侧本轮调整

本轮已在 `utils/order_history_sync.py` 增加保护：

1. 历史订单列表浏览器预热页增加 **无权限页识别**
2. 如果页面已经进入 seller workbench 的 `no-permission` / “当前账号没有访问权限”态：
   - 直接报明确错误
   - 不再继续把问题伪装成普通 Cookie 鉴权失败
3. 当列表 API 返回 `PERMISSION_EXCEPTION` 时，也会再次结合页面正文做判断

### 新增测试

新增测试文件：

- `tests/test_order_history_sync_browser.py`

覆盖点：

1. 预热页直接显示“当前账号没有访问权限”时，历史订单抓取快速失败
2. 列表 API 返回 `PERMISSION_EXCEPTION` 且页面正文已显示无权限时，报错明确落到“卖家工作台无权限访问”

### 对订单链路的实际影响

要分清两件事：

1. **实时订单链路**
   - 主要来源于 IM / WebSocket 消息
   - 识别订单 ID 后再抓订单详情落库
   - 这条链路不依赖 seller 工作台历史列表
2. **历史订单补录链路**
   - 当前实现依赖 seller workbench 的 `merchant.sold.get`
   - 对 `account_id=1` 来说，这条路当前走不通

所以现状是：

- 实时订单 / 后续订单详情链路可以正常工作
- 历史订单列表同步会因为 seller workbench 无权限而失败

### 后续建议

如果后面要继续做“历史订单补录”，不要再默认认定是浏览器画像、Cookie、Token 或 runtime 复用的问题，先看账号对 seller 工作台有没有权限。

更稳的方向有两个：

1. 继续依赖 **实时消息 + 订单详情增量沉淀**
2. 另找 **非 seller.goofish 工作台域** 的历史订单来源，再做接入验证
---

## 2026-05-17 历史订单链路二次收口（回退到 HTTP 列表抓取后真实回归通过）

### 本次结论

这轮已经确认：

1. **2026-05-16 新接入的“managed runtime fresh page + browser fetch 历史订单列表”就是问题点**
2. `account_id=2` 在运行态恢复正常前，历史订单列表无论走浏览器抓取还是 HTTP 抓取，都会因为会话本身还没恢复而报 `Session过期`
3. `account_id=2` 在扫码接管、滑块恢复、Token 初始化全部完成后，**把历史订单列表抓取改回旧的 HTTP 链路，已经真实跑通**

一句话收口：

> 问题不是“账号级画像没复用”，也不是“扫码链路没接管”；真正把历史订单拖死的是那层后加的浏览器列表抓取。去掉它之后，账号恢复完成即可以正常补录历史订单。

### 代码调整

文件：

- `XianyuAutoAsync.py`

本次把：

- `fetch_recent_order_history_candidates()`

从：

- `acquire_runtime(...)`
- `get_fresh_page(...)`
- `history_fetcher.fetch_recent_orders_via_browser(...)`

改回：

- 直接 `history_fetcher.fetch_recent_orders(...)`

同时把 Cookie 同步来源标记从：

- `order_history_sync_browser`

改成：

- `order_history_sync_http`

这次调整保留了“使用当前 live instance 最新 Cookie”这一点，但去掉了“再套一层 fresh page/context”的不稳定因素，行为重新对齐到 5 月 5 日前已经验证过的老链路。

### 新增测试

文件：

- `tests/test_xianyu_async_browser_runtime.py`

新增用例验证：

1. 历史订单列表不会再申请 managed browser runtime
2. 不会再走 `fetch_recent_orders_via_browser`
3. 会直接走 `fetch_recent_orders`
4. 抓取过程中如果 Cookie 有更新，会继续同步回 live instance

本地验证命令：

```bash
python -m pytest tests/test_xianyu_async_browser_runtime.py -k "fetch_recent_order_history_candidates_uses_http_fetcher_with_live_cookies or refresh_cookies_from_qr_login_reopens_account_runtime_when_managed_runtime_lease_already_released"
```

结果：

- `2 passed`

### 本次真实回归（account_id=2）

#### A. 先恢复账号运行态

这轮先对 `account_id=2` 重新发起二维码登录并扫码：

- 二维码会话：`96f07cfb-51a7-4963-a148-8e91c603045f`

关键日志：

- `2026-05-17 02:38:21`
  - `【2】真实Cookie已成功保存到数据库`
- `2026-05-17 02:38:39`
  - `/qr-login/check/...` 返回 `phase=handoff_completed`
- `2026-05-17 02:38:24 ~ 02:39:45`
  - 命中 `FAIL_SYS_USER_VALIDATE`
  - 自动滑块恢复成功
  - Token 刷新恢复成功
- `2026-05-17 02:39:51`
  - runtime 回到 `connected`

最终 `GET /accounts/2/runtime-status` 确认：

- `connection_state = connected`
- `ws_ready = true`
- `session_ready = true`
- `has_current_token = true`
- `token_refresh_status = success`
- `session_keepalive_status = success`

#### B. 再跑历史订单同步

真实调用：

- `POST /api/orders/history-sync`
- `account_id = 2`
- `start_date = 2026-05-01`
- `end_date = 2026-05-17`
- `max_orders = 50`
- `fetch_details = true`

任务：

- `job_id = history_sync_b104df85f6fc3b6f`

结果：

- `status = completed`
- `orders_discovered = 6`
- `matched_orders = 1`
- `orders_processed = 1`
- `orders_saved = 1`
- `orders_failed = 0`

关键日志证据：

- `2026-05-17 02:40:21`
  - `【2】历史订单列表第 1 页抓取完成: page_items=6, scanned=6, in_range=1, before_range=5, after_range=0, unknown_anchor=0, captured=1, totalCount=6, nextPage=False`
- `2026-05-17 02:40:40`
  - 新订单写库成功：
    - `order_id=3299107478187006390`
    - `account_id=2`

同时 `/api/orders` 已能读到这条订单：

- `order_id = 3299107478187006390`
- `account_id = 2`
- `order_status = completed`

### 这次修复真正说明了什么

要把时间线看清楚：

1. **02:34 那次失败**
   - 当时账号运行态还没恢复好
   - 即使已经切到 HTTP 列表抓取，也仍然会报：
     - `历史订单列表 API 调用失败: ['FAIL_SYS_SESSION_EXPIRED::Session过期']`

2. **02:39:51 之后**
   - `account_id=2` 已经重新 `connected`
   - Token / keepalive 都恢复正常

3. **02:40:20 再跑**
   - 历史订单同步完成

因此这次修复的准确结论不是“改完以后任何时刻都能跑”，而是：

> **改完以后，只要账号运行态已经恢复到有效登录态，历史订单链路就不会再被那条浏览器列表抓取链路拖死。**

### 当前建议

如果后面还要继续围绕历史订单补录做增强，默认策略就应该是：

1. **列表优先走 HTTP 抓取**
2. **详情继续允许复用当前账号 runtime**
3. 不要再轻易把“列表抓取”改回需要 fresh page/context 的浏览器链路，除非有新的硬证据证明 HTTP 路径不够用
---

## 2026-05-17 本轮补充：验证处理中态、避免误导兜底按钮、减少二次浏览器拉起

### 1. 人脸/验证材料提交后，后端状态从 `verification_required` 细分为 `verification_processing`

这轮确认的真实链路不是“人脸没过”，而是：

1. 人脸验证已经成功提交
2. 后端继续做 Cookie / Token 稳定化与接管
3. 业务预热阶段可能再次遇到后置滑块
4. 滑块自动处理完成后，才最终进入登录成功 / 链路恢复

之前这里一直返回 `verification_required`，前端就会继续把“打开兜底验证页面”那套按钮亮出来，用户会误以为还得继续手动操作。

本次调整后：

- `reply_server._build_verification_required_status_payload(...)`
  - 当截图已经被消费、验证材料已提交、当前只是在等后台自动收尾时
  - 返回 `status = verification_processing`
- 保留：
  - `verification_pending_completion = true`
  - 不再继续下发可重复点击的兜底验证材料

### 2. 前端三条轮询链路统一识别 `verification_processing`

已覆盖：

- 密码登录轮询
- 刷新 Cookie 轮询
- 手动导入 Cookie 轮询

行为统一改为：

- 如果是 `verification_processing`
  - 展示“验证已提交，系统正在自动完成后续处理，请勿关闭当前窗口”
  - 继续轮询
  - 不再误导性展示兜底验证按钮

### 3. 密码登录后，不再为了 token 失败再二次拉起浏览器

当前策略：

1. 优先用当前登录得到的 Cookie 做 HTTP 预检
2. 如果可以，尝试直接做 token 预热交接
3. 如果 HTTP 预检不通过，再优先复用当前 managed runtime 做 Cookie 稳定化
4. 如果 token 预热仍失败，不再额外再起一轮同账号浏览器去刷
5. 直接进入“延后交接”，让正式实例后续继续完成初始化

本次还顺手修了两个细节：

- token 预热结果既识别 `current_token`，也识别预检协程的返回值
- runtime 如果已经在 handoff 前释放，finally 不再重复释放一遍

### 4. 二维码登录相关测试契约同步更新

测试层面现在明确约束：

- 扫码登录真实 Cookie 落库后，账号任务切换要走 manager loop
- manager loop 不可用时，返回 runtime 问题提示，不再假装 token 已预热成功
- 不再要求扫码阶段同步完成 token 预热

### 5. 账号删除 / 清理时，账号级本地画像与验证截图一起清掉

新增统一清理逻辑：

- `browser_data/user_{account_id}`
- `static/uploads/images/face_verify_{account_id}_*.jpg|png`

并挂到：

- `remove_cookie(account_id)`

### 6. 本轮新增/更新测试

覆盖点：

- `tests/test_reply_server_account_scope.py`
  - `verification_processing` 状态构造
  - 前端处理 `verification_processing` 的契约
  - 删除账号时画像/验证截图一起清理
- `tests/test_reply_server_manual_cookie_import_flow.py`
  - 密码登录 handoff 释放时机
  - token 预热失败时走“延后交接”而不是二次浏览器刷新
  - 扫码登录在 manager loop 场景下的切换行为

### 7. 本轮本地验证结果

执行：

```bash
python -m pytest tests/test_reply_server_manual_cookie_import_flow.py
python -m pytest tests/test_qr_login_status_flow.py
python -m pytest tests/test_reply_server_account_scope.py -k "verification"
```

结果：

- `16 passed`
- `40 passed`
- `5 passed`

### 8. 回归前现场清理

为避免旧数据串味，本轮回归前已清空：

- `data/xianyu_data.db`
- `data/qr_login/*`
- `logs/*`
- `browser_data/*`
- `static/uploads/images/*`
- `trajectory_history/*`
- `tmp_qr_*.png`
- `realtime.log`
- `tmp_service_stdout.log`
- `tmp_service_stderr.log`

并确认当时残留的两个 `Start.py` 进程已全部停止后再重启服务。

---

## 2026-05-18 补充：编辑账号资料后误触发账号重启的问题

## 2026-05-18 补充：删除“显示浏览器窗口”开关，账密/扫码相关流程统一无头

### 1. 本次调整目标

把前后端残留的“显示浏览器窗口 / show_browser”彻底逻辑删除，避免：

- 本机调试和正式 Docker 行为不一致
- 账密登录、手动导入 Cookie、手动刷新 Cookie、自动恢复之间混用有头/无头
- 前端还留着开关，实际上后端很多入口早就强制无头，造成误导

### 2. 本次实际改动

- 前端删除：
  - 账号编辑中的“显示浏览器”
  - 账密登录中的“显示浏览器窗口”
  - 手动导入 Cookie 中的“显示浏览器窗口”
  - 手动刷新 Cookie 中的“显示浏览器窗口”
- 前端请求体删除：
  - 不再向 `/password-login`
  - `/manual-cookie-import`
  - `/accounts/{account_id}/account-info`
  传 `show_browser`
- 后端删除：
  - `ManualCookieImportRequest.show_browser`
  - `CookieAccountInfo.show_browser`
  - 账密登录 / 手动导入 Cookie / 会话状态 / 风控元数据里对 `show_browser` 的透传和记录
- 运行态统一：
  - 账密登录：固定 `headless=True`
  - 手动 Cookie 导入验证：固定 `headless=True`
  - Cookie 失效后的自动恢复 / token 刷新：固定 `headless=True`
  - 二维码验证页：固定 `headless=True`

### 3. 兼容策略

本轮采用 **A 方案：逻辑删除，数据库字段保留**：

- `cookies.show_browser` 字段暂时不做物理删列
- 但已经不再：
  - 读这个字段
  - 写这个字段
  - 返回给前端

这样能避免为了删一个历史字段去做 SQLite 迁移，平白增加翻车面。

### 4. 验证结果

执行：

```bash
python -m py_compile reply_server.py db_manager.py XianyuAutoAsync.py utils/qr_login.py
.venv\Scripts\python.exe -m pytest tests/test_db_manager_account_id_relations.py tests/test_reply_server_account_scope.py -q
```

结果：

- `py_compile` 通过
- `131 passed`
- `19 subtests passed`

### 5. 当前结论

- 后续正式环境不再存在“勾错开关导致切到有头模式”的问题
- 账密登录、Cookie 导入、自动恢复、二维码验证页的浏览器启动策略统一为无头
- 如果后面还要排登录 / 滑块 / 人脸问题，就应该直接查无头链路本身，不要再怀疑这个已删除的前端开关

### 1. 现象

- 前一轮 `account_id=1` 扫码登录已经成功，`Cookie -> Token -> WebSocket` 链路都正常；
- 之后只是在账号管理里给这个账号补了用户名密码，结果运行态被打断，紧接着又进入滑块/风控恢复链路；
- 用户体感上就是：**“刚刚明明连上了，编辑一下账号资料反而失败了。”**

### 2. 根因

不是 `POST /accounts/1/account-info` 把 cookie 覆盖坏了，真正的问题是：

- 前端保存账号资料时，会顺手再调一次 `POST /accounts/1/proxy`
- 后端旧逻辑里，**代理配置接口不管代理有没有变化，都会直接重启账号任务**
- 于是刚恢复好的运行态被人为打断，立刻重新走：
  - runtime 退出
  - token refresh
  - 可能触发滑块/风控

也就是说，**“编辑账号资料后失败”本质上不是资料保存失败，而是代理保存接口的无脑重启副作用。**

### 3. 日志证据

日志文件：

- `logs/xianyu_2026-05-18.log`

关键时间点：

1. `2026-05-18 00:32:24.615`
   - `POST /accounts/1/account-info`
2. `2026-05-18 00:32:24.640`
   - `Cookie值未变化，无需重启任务: 1`
   - 说明：**补用户名密码这一步本身没有触发 cookie 重启**
3. `2026-05-18 00:32:24.650`
   - `POST /accounts/1/proxy`
4. `2026-05-18 00:32:24.657`
   - `代理配置已更新，重启账号任务: 1`
   - 说明：**真正把运行态打断的是代理配置接口**

后面日志马上就能看到：

- 旧实例停止
- WebSocket 退出
- 重新进入 token / 滑块恢复链路

### 4. 修复

涉及文件：

- `reply_server.py`
- `tests/test_reply_server_proxy_restart_guard_contract.py`

新增逻辑：

1. 给代理配置加统一归一化快照 `_normalize_proxy_config_snapshot()`
2. 先比较“当前配置”和“目标配置”
   - **完全没变化：直接返回，不重启任务**
3. 再比较“实际生效配置”
   - 对 `proxy_type=none` 的情况，把 `host/port/user/pass` 统一折叠为空值后再比较
   - 避免只是清理残留字段，也误判成“需要重启”
4. 只有**实际生效代理真的变化了**，才重启账号任务

一句话：

> **编辑账号资料时，如果代理没变，就不该把已经跑稳的账号运行态狠狠干掉。**

### 5. 本轮验证

执行：

```bash
python -m py_compile reply_server.py
python -m pytest tests/test_reply_server_proxy_restart_guard_contract.py -q
```

结果：

- `py_compile` 通过
- `2 passed`

---

## 2026-05-18 本次账密真实托管回归（account_id=1）

这次不是走正式服务接口回归，而是直接用手工托管入口复现真实链路：

- 入口：`debug_manual_password_login.py`
- 账号：`account_id=1`
- 用户名：`15614318625`
- 密码：`qq1205747671`
- 模式：`--headless`

### 1. 先确认的真实问题

前一版代码里，账密登录在人脸通过后，后续 `token_refresh` 业务预热命中 `punish` / `x5step=2` 滑块时，虽然：

- 滑块本身已经自动通过
- `x5sec` / `x5secdata` 已刷新
- 页面也已经退出验证页

但代码没有把这条链路当成“可以继续收口”的成功分支，反而又掉回 `_process_verification_requirement(...)` 的人工验证等待分支，导致看起来像：

- 人脸明明扫完了
- 滑块也过了
- 结果程序还在继续等“身份验证”

这是假阻塞，不是真没过。

### 2. 本次代码调整

涉及文件：

- `utils/xianyu_slider_stealth.py`
- `reply_server.py`
- `tests/test_reply_server_account_scope.py`

本次新增/收口：

1. **浏览器业务预热验证页自动续解后，允许直接回到 Cookie 收口**
   - 位置：`_consume_browser_cookie_warmup_verification_hint(...)`
   - 逻辑：
     - 如果 `token_refresh` 验证页里的滑块已经自动通过
     - 且验证页已经退出
     - 且 Cookie 出现了有效刷新（如 `x5sec` 变化）
   - 则不再重新掉回人工验证等待，而是直接重新进入 `_finalize_logged_in_cookies(...)`

2. **清理 warmup handoff 状态**
   - 自动续解成功后，主动清空：
     - `last_browser_cookie_warmup_verification_hint`
     - `last_browser_cookie_warmup_session_unready`
   - 避免旧的 warmup 验证提示残留，后面又把流程拽回错误分支

3. **只缺 `cna` 时允许按业务就绪态交接**
   - 位置：`reply_server.py`
   - 当浏览器业务预热已证明会话可用，且缺失仅剩 `cna` 时：
     - 允许本次账密交接成功
     - 后续由正式实例继续观察/补齐

4. **补测试**
   - 新增覆盖：
     - 预热验证页自动滑块成功后直接回收口
     - IM 空会话页按已登录处理
     - pending identity marker 在业务已就绪时不再二次拉验证页

### 3. 本次真实回归结果

本轮真实无头托管链路结果：

1. 账密登录进入人脸验证
2. 人脸扫码完成
3. 后续命中 `token_refresh` 风控滑块
4. 滑块自动通过
5. 返回 `https://www.goofish.com/im`
6. 页面识别到 **IM 空会话登录态**
7. 业务预热成功：
   - `login_token_fetch` 成功
   - `login_user_fetch` 成功
   - `userId=2095002164`
8. 清理 pending identity markers：
   - `ivActionType`
   - `tmp0`
   - `siv20`
   - `last_u_xianyu_web`
9. 最终返回成功 Cookie

命令输出结果：

- `success=True`
- `last_login_error=`
- `cookie_count=21`
- `has_x5sec=True`

### 4. 仍可继续优化的点

这轮虽然成功，但还看到一个可收紧点：

- `人脸验证验证完成稳定化动作: goto_home -> https://www.goofish.com/`
- 有一次 `Timeout 15000ms exceeded`
- 不影响最终成功，但会白等一段时间

后续可以继续优化：

1. 减少 `goto_home` 这类对最终成功帮助不大的稳定化动作
2. 在已确认 `login_token_fetch + login_user_fetch` 均成功时，更早结束无效页面预热

### 5. 本次验证命令

执行：

```bash
.\.venv\Scripts\python.exe -m py_compile utils\xianyu_slider_stealth.py tests\test_reply_server_account_scope.py
.\.venv\Scripts\python.exe -m pytest tests\test_reply_server_account_scope.py -q
.\.venv\Scripts\python.exe .\debug_manual_password_login.py --account-id 1 --account 15614318625 --password qq1205747671 --headless --verification-wait-timeout 900 --keep-verification-screenshot
```

结果：

- `py_compile` 通过
- `102 passed`
- 手工托管账密真实回归成功

### 6. 关于“服务是否已经更新”

这次要分清两件事：

1. **仓库代码已经更新**
   - 上面这些修复已经改进当前工作区代码

2. **正式服务进程这次没有跟着一起重启验证**
   - 我刚才跑的是 `debug_manual_password_login.py` 手工托管链路
   - 不是 `Start.py` / `reply_server.py` 正式服务接口在跑

所以准确说法是：

> **代码已经改了，但正式服务进程如果还没重启，就还没加载这次新代码。**
## 2026-05-18 本次结构收口（business-ready / verification recovery / finalize）

这次不是继续堆补丁，而是把已经跑通的账密登录收口逻辑做了最小结构整理，目标是：
- 保持现有行为不变
- 降低 utils/xianyu_slider_stealth.py 内部重复逻辑
- 让后续继续改登录链路时，不至于两边规则漂移、越改越邪门

### 1. 统一 business-ready 判定

位置：utils/xianyu_slider_stealth.py

新增并收口：
- _has_business_ready_cookie_shape(...)
- _has_browser_cookie_warmup_probe_business_ready(...)
- _should_accept_business_ready_cookie_handoff(...)

调整后：
- 浏览器 warmup 已证明 login_token_fetch + login_user_fetch 成功
- 且 Cookie 已具备业务可用形态（允许只差 cna）
- 则统一按“business-ready handoff”处理

同时：
- eply_server.py 中 _should_accept_password_login_business_ready_handoff(...)
- 不再自己维护一套“只差 cna 是否可放行”的规则
- 改为复用 slider_instance._should_accept_business_ready_cookie_handoff(...)

这样避免两边各写一套判断，后面再改一次又分叉。

### 2. 抽验证恢复 helper

位置：utils/xianyu_slider_stealth.py

新增：
- _recover_verification_url_with_auto_slider_then_finalize(...)

收口前，下面两条链路都各自复制了一遍：
1. 打开验证 URL
2. 检测 slider / pureCaptcha
3. 尝试自动滑块
4. 成功后回 inalize
5. 不成功再落入人工验证等待

现在统一由 helper 处理，覆盖：
- _handle_pending_identity_verification_state(...)
- _consume_browser_cookie_warmup_verification_hint(...)

其中 warmup verification hint 那条链路仍保留原先的重要行为：
- 自动滑块后如果验证页已退出
- 且 Cookie 出现有效刷新
- 即便当下还没立即探测到明确登录页，也允许直接回到 inalize 收口

### 3. 瘦身 _finalize_logged_in_cookies()

位置：utils/xianyu_slider_stealth.py

原来 _finalize_logged_in_cookies() 里面把：
- Cookie 快照
- Set-Cookie 合并
- stabilize
- browser warmup
- warmup verification hint 消费
- pending identity 处理
- 最终失败/成功收口

全堆在一个函数里，读起来挺埋汰。

这次拆成 3 段内部 helper：
- _collect_logged_in_cookie_snapshot(...)
- _stabilize_and_warmup_logged_in_cookies(...)
- _finalize_cookie_handoff_or_fail(...)

作用是：
- 只做文件内结构收口
- 不改对外行为
- 让后面继续查“为什么成功后又多走一步”“为什么少字段还能交接”时，定位更直接

### 4. 本次验证

执行：
`ash
.\.venv\Scripts\python.exe -m py_compile .\utils\xianyu_slider_stealth.py .\tests\test_reply_server_account_scope.py .\reply_server.py
.\.venv\Scripts\python.exe -m pytest .\tests\test_reply_server_account_scope.py -q
`

结果：
- py_compile 通过
- 102 passed
- 9 subtests passed

### 5. 结论

这次改动不是新增功能，而是把前面已经确认正确的账密登录成功链路做结构收紧：
- business-ready 规则不再双份维护
- 验证恢复逻辑不再复制粘贴
- finalize 收口函数职责更清楚

后续如果还要继续往二维码登录、Cookie 导入登录、token 刷新滑块恢复上复用，这次收口会省不少脏活。

---

## 2026-05-18 补充：直接托管浏览器实测账密登录后，Cookie 入库 + WS/心跳全链路打通

### 1. 这次先把一个容易混淆的点钉死

`utils/xianyu_slider_stealth.py` 里的 `login_with_password_browser()` 只负责两件事：

1. 用 account 级 persistent profile 跑账密登录
2. 在登录成功后拿到当前浏览器上下文里的真实 Cookie

它**不负责正式入库**。

真正把账密登录结果写入数据库、绑定 `user_id`、补账号信息并启动后续链路的收口逻辑，在：

- `reply_server.py` -> `_persist_password_login_success(...)`

之前本地调试时之所以一度看起来“浏览器都关了、数据肯定也保存了”，其实那只是：

- 浏览器 profile 已落盘

不是：

- 数据库里的 Cookie 已正式保存

这个坑要记住，不然后面再看“浏览器数据在、数据库没数据”的现象时，又容易瞎判断。

### 2. 这轮真实托管回归结论

本轮使用无头托管方式直接跑 `accountid=5` 账密登录，后面不走服务接口中转，直接验证登录成功后的整条链路。

确认结果：

- 登录成功
- 成功拿到真实 Cookie
- Cookie 正式入库成功
- `bound_unb` 正常
- token 预热成功
- WebSocket 建连成功
- heartbeat 正常
- session keepalive 正常

关键结果摘要：

- `success=True`
- `cookie_count=19`
- `has_x5sec=True`
- `cookie_persisted_in_db=True`
- `cookie_persist_user_id=1`
- `preflight_token_received=True`
- `ws_connected=True`
- `ws_connection_state=connected`
- `session_keepalive_ok=True`

数据库侧也已确认：

- `accountid=5` 存在有效 Cookie
- `user_id=1`
- `bound_unb=2095002164`
- `username=15614318625`

### 3. 这轮代码调整点

涉及文件：

- `reply_server.py`
- `utils/xianyu_slider_stealth.py`
- `tests/test_reply_server_account_scope.py`

本轮主要不是改登录大结构，而是把“验证页退出后到最终登录收口”这段补齐：

1. **验证材料提交后，前端状态进入 `verification_processing`**
   - 后端通过 `verification_pending_completion` 显式标记“验证已提交，正在自动收口”
   - 这个阶段不再继续给前端展示“打开辅助链接”按钮

2. **验证页退出后主动推送 processing 状态**
   - `utils/xianyu_slider_stealth.py` 新增 `_notify_verification_processing(...)`
   - 避免用户明明已经扫完，页面还卡在“仍需验证”的错觉里

3. **页面已明显呈现登录态时，允许更早 handoff 到 Cookie finalize**
   - 即便某些 `pending identity marker` Cookie 还没完全消失
   - 只要验证页已经退出、页面已进入可识别登录态，就不再傻等

4. **补测试覆盖**
   - 显式 `verification_pending_completion` 状态构造
   - 验证页退出后的 processing 通知
   - pending marker 尚存但页面已登录时直接 handoff

### 4. 当前剩余可继续优化点

本轮链路已经打通，但还能继续抠一个性能点：

- 登录成功后的“已登录确认”还有点偏慢

现象是：

- 页面实际上已经到 IM 登录态
- 但 `.rc-virtual-list-holder-inner` 这类列表容器还没完全稳定
- 于是探测逻辑会多绕几轮才进入最终收口

后续优化方向应该是：

1. 利用更早的 IM 登录态信号提前认定成功
2. 更早 handoff 到 Cookie finalize
3. 不动已经跑通的人脸 / 滑块 / token / WS 主线

也就是说，后面该优化的是“确认时机”，不是再去乱拆已通的验证链路。
