# Token Refresh Hydrated Profile Design

## 背景

`token_refresh` 场景命中阿里 `punish?action=captcha` 后，现有链路会创建新的 `XianyuSliderStealth` 实例，并走 `launch(...) + new_context() + add_cookies()` 的临时上下文流程。

本地实验已经确认：

- 旧 Cookie + 新建临时上下文：稳定失败
- 旧 Cookie + 新建 persistent profile：仍然失败
- 旧 Cookie + 复用已养熟的账号浏览器 profile：可以自动通过

结论不是“轨迹参数不够骚”，而是 `token_refresh` 的自动恢复缺失了账号级浏览器连续性。

## 目标

让 `token_refresh` 命中的滑块恢复链，优先复用账号级 hydrated persistent profile，从而继承账号已有的浏览器状态、站点状态和风控信任；同时保留现有临时上下文链路作为兜底，避免因为 profile 锁冲突直接把恢复链打死。

## 非目标

- 不重写账密登录主链
- 不重构轨迹学习逻辑
- 不改变手动 Cookie 导入主链默认策略
- 不移除现有 `token_refresh -> hard reject -> 账密恢复` 的外层收口

## 方案

### 1. 调用侧

在 `XianyuAutoAsync._handle_captcha_verification(...)` 创建 `XianyuSliderStealth` 时，显式打开“账号级 persistent profile 优先”模式。

这样只影响 `token_refresh` 场景，不把行为偷偷扩散到别的入口。

### 2. `XianyuSliderStealth` 初始化策略

为 `XianyuSliderStealth` 增加账号级 persistent profile 配置：

- `use_account_persistent_profile`
- `account_persistent_profile_dir`

当该模式开启时，`init_browser()` 改为优先：

1. 解析 `browser_data/user_<account_id>`
2. `launch_persistent_context(...)`
3. 继续沿用现有浏览器画像、代理、Cookie 注入、预热与后续滑块流程

如果 persistent profile 启动失败，且属于“profile 被占用”这类已知锁冲突，则回退到当前的 `launch(...) + new_context()` 临时上下文链路。

### 3. 行为边界

- 优先复用账号 profile，但不要求 profile 目录一定已存在
- profile 存在但未养熟，不保证立刻变稳；本次改动先把“能复用已养熟 profile”这条能力补齐
- fallback 只作为兜底，不改变已有失败语义

## 错误处理

- persistent profile 目录不存在：自动创建并继续
- persistent profile 被锁：记录 warning，回退旧链路
- 其他启动异常：沿用现有异常处理与通知收口

## 测试策略

1. 调用侧测试：`token_refresh` 创建滑块实例时，显式传入账号 persistent profile 选项
2. 初始化测试：开启该选项时，`init_browser()` 优先走 `launch_persistent_context(...)`
3. 回归测试：保留现有 `token_refresh` / 滑块 hard reject / 学习样本相关测试

## 风险

- profile 目录若被外部 Chromium 长时间占用，仍可能退回旧链路，稳定性取决于环境
- 账号 profile 复用会让 `browser_data/` 更重要，部署文档需要明确“这是运行期状态，不要乱清”

## 验收标准

- `token_refresh` 滑块恢复链代码层面优先复用账号级 persistent profile
- 单测覆盖调用侧和初始化分支
- 文档明确该链路的新策略、fallback 行为和 `browser_data/user_<id>` 的作用

## 额外运行时保护（2026-05-05 stale lock 补充）

- 如果 Chromium `SingletonLock` 明确指向**当前宿主机**，且记录的 PID 已不存在，则将其视为 stale singleton 锁
- 如果宿主机名不一致，但锁宿主和当前宿主都长得像 12 位十六进制 Docker 容器 hostname，且 PID 已不存在，则也视为“旧容器 hostname 漂移”造成的 stale 锁
- 只有在这类可证明 stale 的场景下，才自动清理 `SingletonLock` / `SingletonCookie` / `SingletonSocket`
- 清理后只重试一次 `launch_persistent_context(...)`
- 如果宿主机不匹配、PID 仍存活，或锁信息无法解析，则绝不自动删除，继续走原有 fallback
