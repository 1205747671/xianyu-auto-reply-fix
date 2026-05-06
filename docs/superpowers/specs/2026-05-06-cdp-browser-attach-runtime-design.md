# CloakBrowser CDP 接管浏览器运行时设计

日期：2026-05-06

## 1. 背景

当前项目虽然已经切到 `CloakBrowser`，但活跃链路仍然主要走“业务代码直接 launch / launch_persistent_context，然后在 Playwright page 上继续自动化操作”这一套。

这带来两个实际问题：

1. **浏览器指纹和自动化交互仍然耦合在一起**
   - 浏览器本体是否像真人，和业务代码怎么点、怎么拖、怎么等待，混在同一层。
   - 出问题时很难判断是指纹问题，还是交互问题。

2. **滑块链路存在双重行为补丁风险**
   - 项目自己的滑块轨迹逻辑，叠加 `CloakBrowser humanize` 对 `page.mouse.*` 的补丁，容易出现动作被二次“人化”的情况。
   - 当前证据已经说明：同一个指纹浏览器里，手工新开窗口可以过，程序自动拖不过，说明主要矛盾已经转向“自动化交互方式”。

所以这次不再只改滑块，而是把**整个浏览器运行时切成“外部受管浏览器进程 + Playwright CDP attach”**。

## 2. 目标

本次设计目标：

1. **程序自行启动 CloakBrowser 浏览器进程**
2. **再通过 `connect_over_cdp()` 接管整个浏览器**
3. **统一下列链路到同一个 CDP 浏览器运行时**
   - 账号密码登录
   - 扫码登录
   - 手动导入 cookie
   - 风控验证承接（二维码 / 人脸 / 滑块后续等待）
   - `token_refresh` 浏览器恢复链路
4. **保留账号级持久 profile**
   - 继续使用 `browser_data/user_<account_id>`
5. **浏览器被手动关闭时快速失败**
   - 不能继续傻等验证码或会话状态

## 3. 非目标

这次先不做下面这些：

1. 不直接改 Linux / Docker 生产部署行为  
   先本地非 Docker 跑通，再同步 Linux。

2. 不先改所有旁路功能  
   搜索旁路、订单详情抓取等非核心链路，可以在主登录链路验证通过后再逐步切。

3. 不把“手工开着的任意浏览器实例”作为正式运行模式  
   本次只做“**程序自己启动，再由 CDP 接管**”。

## 4. 方案对比

### 方案 A：继续直接使用 `launch_persistent_context`

优点：
- 现有改动最少

缺点：
- 浏览器启动和自动化控制仍然绑死
- 滑块行为和 `humanize` 仍可能继续打架
- 很难复刻“浏览器本体没问题，只是程序交互不对”的真实场景

结论：不选。

### 方案 B：程序启动 CloakBrowser 进程，再 `connect_over_cdp`

优点：
- 浏览器进程和控制层解耦
- 更贴近“真实用户在浏览器里操作”的运行边界
- 便于以后把不同登录方式、cookie 恢复方式统一到同一个浏览器 runtime
- 能独立控制是否启用 `humanize`

缺点：
- 需要新增进程管理、端口发现、CDP attach、断连清理

结论：**推荐方案，采用。**

### 方案 C：附着到用户手工打开的浏览器实例

优点：
- 最接近人工实操现场

缺点：
- 会话归属混乱
- 难做稳定复现
- 不适合作为正式链路

结论：可以保留为调试手段，但不作为主实现。

## 5. 总体设计

### 5.1 两层结构

后续浏览器层改成两层：

1. **Browser Process Layer**
   - 负责启动 `CloakBrowser` Chromium 进程
   - 负责 profile 路径、代理、调试端口、进程生命周期

2. **CDP Control Layer**
   - 负责通过 Playwright `connect_over_cdp()` 接管浏览器
   - 负责拿默认 context / page
   - 负责业务登录、验证等待、cookie 注入、状态检查

两层分开后，业务代码不再假设“我自己创建了 page，所以 page 的行为模型一定就是 launch 时那套”。

### 5.2 启动方式

程序不再通过 `launch_browser_persistent_context()` 直接拿 context 来跑主登录链路，而是：

1. 获取 CloakBrowser runtime 二进制路径
2. 构造 Chromium 启动参数
3. 增加：
   - `--remote-debugging-port=0`
   - `--remote-debugging-address=127.0.0.1`
4. 使用账号级 `user_data_dir`
5. 启动独立浏览器进程
6. 读取 profile 目录中的 `DevToolsActivePort`
7. 拼出 CDP endpoint，例如：
   - `http://127.0.0.1:<port>`
8. 由 Playwright `chromium.connect_over_cdp()` 接管

这样做的关键点是：**浏览器是一个真实存在的独立进程，Playwright 是后来 attach 上去的控制器。**

## 6. 运行时组件设计

### 6.1 `utils/browser_provider.py`

新增/重构为统一浏览器运行时提供者，核心职责：

1. **构造启动参数**
   - 复用 CloakBrowser 的 stealth 参数生成逻辑
   - 透传 locale / timezone / proxy / channel 风格配置

2. **启动受管浏览器进程**
   - 使用 `subprocess.Popen(...)`
   - profile 指向 `browser_data/user_<account_id>`
   - 记录 pid、user_data_dir、debug_port、启动时间

3. **发现 CDP 端点**
   - 优先读取 `DevToolsActivePort`
   - 轮询等待浏览器就绪

4. **CDP attach**
   - `sync_playwright().start()`
   - `playwright.chromium.connect_over_cdp(endpoint)`

5. **运行时清理**
   - 关闭 Browser / Playwright
   - 必要时回收浏览器子进程

建议暴露一个清晰的数据结构，例如：

- `ManagedBrowserRuntime`
  - `process`
  - `playwright`
  - `browser`
  - `context`
  - `endpoint_url`
  - `user_data_dir`
  - `pid`

### 6.2 `utils/xianyu_slider_stealth.py`

主链路不再直接关心浏览器是怎么 launch 的，只关心：

1. 我要一个可用的 `context`
2. 我要一个可用的 `page`
3. 这个 runtime 是否仍然活着

因此要把下面这些入口统一切到 CDP runtime：

1. `账号密码登录`
2. `扫码登录`
3. `手动导入 cookie`
4. `token_refresh` 的浏览器恢复
5. 验证页等待 / 收口 / cookie 提取

## 7. 各条链路如何接入

### 7.1 账号密码登录

当前链路中最容易出问题的点，就是浏览器启动和 page 行为补丁缠在一起。

切换后流程：

1. 启动受管 CloakBrowser 进程
2. CDP attach
3. 获取默认 context / page
4. 打开登录页
5. 输入账号密码
6. 接管滑块 / 二维码 / 人脸验证
7. 登录成功后提取 cookies

关键约束：

- **CDP 接管模式下默认关闭 `humanize`**
  因为当前项目已经有自己的滑块轨迹和节奏逻辑，再叠一层很容易把动作搞残。

### 7.2 扫码登录

扫码登录也必须切到同一个运行时，不然会出现两套浏览器状态：

1. 一套是扫码页
2. 一套是后续保活 / cookie 提取页

切到 CDP runtime 后：

1. 用同一个账号 profile 打开登录页
2. 切二维码登录
3. 等用户扫码
4. 在同一个 context 内等待会话落地
5. 提取 cookies 并保存

### 7.3 手动导入 cookie

手动导入 cookie 不能只做“文本写库”，还应该接同一个浏览器 runtime 做会话校验。

建议流程：

1. 用户提交 cookie 文本
2. 启动/接管账号对应的 CDP 浏览器
3. 向 context 注入 cookies
4. 打开业务页验证会话是否真实有效
5. 如有补票据（如 `x5sec`、`_m_h5_tk` 等），再做二次快照
6. 校验通过后再正式落库

这样能避免“手动导入看着成功，实际上只是写进数据库，浏览器会话根本没活”的假成功。

### 7.4 `token_refresh`

`token_refresh` 也要切进同一个 runtime 框架，因为它本质上也是：

1. 起浏览器
2. 恢复 profile / cookie
3. 检测是否命中验证页
4. 承接滑块 / 二维码 / 人脸
5. 成功后更新 cookie

这条线如果还留旧启动模式，后续一定继续分叉。

## 8. 浏览器关闭与失活处理

这次必须补这一层，不然还会出现“窗口都没了，代码还在等”的蠢事。

需要新增以下检测：

1. **Browser disconnect 事件**
2. **Page / Context close 异常快速收口**
3. **受管子进程退出检测**
4. **等待验证码时的 fail-fast**

处理规则：

- 一旦发现浏览器进程退出、CDP 断连、context 已关闭：
  - 立即中止等待
  - 设置明确错误信息
  - 返回“浏览器会话已关闭/失效”

## 9. 测试设计

### 9.1 单元测试

新增/调整的测试重点：

1. CDP 端口发现逻辑
   - `DevToolsActivePort` 读取
   - 端口等待超时

2. CDP attach 运行时装配
   - browser/context/page 选择
   - attach 失败收口

3. 浏览器关闭 fast-fail
   - 等验证时窗口被关掉，应尽快失败

4. 手动导入 cookie 校验链路
   - 注入 -> 校验 -> 快照 -> 落库

### 9.2 本地手工验证

本地非 Docker 验证顺序：

1. 账号密码登录
2. 扫码登录
3. 手动导入 cookie
4. `token_refresh`

重点观察：

1. 是否正常 attach 到 CDP 浏览器
2. 登录页是否稳定
3. 滑块失败码是否继续变化
4. 二维码 / 人脸验证是否能正确承接
5. 手动关闭浏览器后是否快速退出

## 10. 风险与缓解

### 风险 1：CDP attach 后默认 context/page 选择不稳定

缓解：

- 明确选择默认 context
- 明确识别当前活动 page
- 必要时在 attach 后新建受控 page

### 风险 2：启动进程后端口发现慢

缓解：

- 轮询 `DevToolsActivePort`
- 给清晰超时和错误日志

### 风险 3：旧逻辑仍有残留 launch 分支

缓解：

- 先从四条核心链路统一切：
  - 账号密码登录
  - 扫码登录
  - 手动导入 cookie
  - `token_refresh`
- 其余旁路后续再清

## 11. 实施顺序

建议按下面顺序做：

### Phase A

先实现 CDP runtime 基础设施：

1. 受管浏览器进程启动
2. 调试端口发现
3. `connect_over_cdp`
4. 断连 / 关闭收口

### Phase B

切主登录链路：

1. 账号密码登录
2. 滑块承接
3. 二维码 / 人脸等待

### Phase C

切其余核心链路：

1. 扫码登录
2. 手动导入 cookie
3. `token_refresh`

### Phase D

本地验证通过后，再同步 Linux / Docker。

## 12. 最终结论

本次不再把问题理解成“换了指纹浏览器就万事大吉”，而是明确拆成两件事：

1. **浏览器本体**
   - 由 CloakBrowser 负责

2. **程序如何接管和操作这个浏览器**
   - 由 CDP attach 运行时负责

最终正式方案是：

> **程序启动 CloakBrowser 浏览器进程，开启 remote debugging，再由 Playwright `connect_over_cdp()` 接管整个浏览器；账号密码登录、扫码登录、手动导入 cookie、验证承接、`token_refresh` 统一迁到这套运行时。**
