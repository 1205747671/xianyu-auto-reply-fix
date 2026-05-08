# Account Browser Runtime Unification Design

**日期：** 2026-05-08

## 背景

`CLOAKBROWSER_BROWSER_SCOPE_REQUIREMENTS_20260507.md` 对“浏览器作用域”已经提出了明确要求：浏览器身份、验证、Cookie 获取、恢复链路应尽量收敛到同一套受管运行时内完成，避免业务链路各自起浏览器、各自落状态。

当前仓库已经完成了部分 CloakBrowser 接入，但账号与浏览器边界仍不够硬，主要问题集中在三类：

1. **账号主键与浏览器主键未彻底统一**
   - 账号密码登录、手动 Cookie 导入已经显式带 `account_id`
   - 扫码登录仍存在“先扫码成功，再反推账号”的分支
   - `unb` 目前既承担平台身份识别，又在局部链路里承担运行时寻址，职责混杂
2. **浏览器 runtime 有底座，但主链路和旁路链路没有全部收口**
   - `utils/browser_provider.py` 已具备 CDP attach / managed runtime 能力
   - 但 `utils/xianyu_slider_stealth.py`、`utils/qr_login.py`、`utils/item_search.py`、`utils/order_detail_fetcher.py`、`XianyuAutoAsync.py` 仍存在直接 `launch_browser_*` 或自建 context 的路径
3. **旁路浏览器能力破坏账号隔离**
   - `utils/item_search.py` 仍会拿“第一个有效 cookie”并使用共享缓存目录
   - `utils/order_detail_fetcher.py` 仍以原始 `cookie_string` 驱动浏览器详情抓取
   - 手动刷新、验证恢复等会修改状态的操作尚未统一并发与回滚策略

如果继续保留这套“半统一”状态，系统很容易出现以下问题：

- 同一个账号不同登录方式落到不同浏览器状态
- 搜索/抓单串用到其他账号的登录态
- 验证完成后状态落进临时 profile，主任务仍然读不到
- fallback 看似成功，实际已经偷偷换了身份

## 目标

本轮设计目标是把“账号身份”和“浏览器身份”统一成同一套受控模型，并为后续实施提供清晰边界。

具体目标：

1. **统一账号主键**
   - 系统内部唯一账号主键为 `account_id`
   - `account_id` 创建后锁死，不允许修改
2. **统一浏览器主键**
   - 每个 `account_id` 对应唯一账号级浏览器 runtime
   - 每个 `account_id` 对应唯一持久化画像目录 `browser_data/user_<account_id>`
3. **统一登录链路**
   - 账号密码登录、扫码登录、手动 Cookie 导入全部先确定 `account_id`，再进入该账号 runtime
4. **统一旁路浏览器能力**
   - 搜索、订单详情、手动刷新、验证恢复等所有浏览器能力必须显式指定 `account_id`
   - 不再允许“第一个有效 cookie”“共享缓存目录”“临时匿名 context”式兜底
5. **统一失败与回收策略**
   - fallback 只能重建同账号 runtime，不允许更换身份
   - 明确并发锁、回滚、失效判定、空闲回收、日志观测规则

## 非目标

本设计不包含以下内容：

1. 不在本轮重写数据库 schema；仅在现有账号记录上补强约束与字段语义
2. 不在本轮引入多账号共享浏览器池
3. 不在本轮放开同账号多浏览器并发；一期以稳定优先，默认串行
4. 不在本轮改造非浏览器业务逻辑，如消息处理、自动回复策略、订单规则判定

## 方案选择

### 方案 A（采用）：`account_id` 作为唯一内部主键，统一账号级 runtime

- 所有浏览器动作都显式传 `account_id`
- 所有 runtime、profile、锁、日志都以 `account_id` 为中心
- `unb` 只承担平台身份绑定与串号校验，不参与 runtime key 计算

**优点**

- 身份边界清晰，最容易防串号
- 与当前已有账号管理模型兼容
- 浏览器生命周期、回滚、排障都能围绕一个稳定 key 做

**缺点**

- 需要改动所有浏览器相关入口
- 需要把现有“按 cookie 串临时起浏览器”的接口全部收口

### 方案 B：继续保留 `cookie` / `unb` / `account_id` 混合寻址

- 某些链路用 `account_id`
- 某些链路用 `cookie_string`
- 某些链路扫码成功后按 `unb` 匹配账号

**不采用原因**

- 职责混乱，后续任何 fallback 都容易串身份
- 不利于统一 runtime 管理器与并发策略

### 方案 C：以 `unb` 作为浏览器 runtime 主键

- 扫码后拿到 `unb`，以 `unb` 作为 profile/runtime key
- `account_id` 只做业务层展示和数据库映射

**不采用原因**

- 新增账号扫码前还没有稳定 `unb`
- `unb` 是平台身份，不是系统主键
- 一旦允许 runtime key 从“未知”变为“已绑定 `unb`”，将引入 profile 迁移与状态漂移问题

**结论：采用方案 A。**

## 核心模型

### 1. 账号主键：`account_id`

- `account_id` 是系统内部唯一账号主键
- 所有登录、验证、恢复、搜索、抓单、刷新等浏览器相关动作，都必须显式绑定到某个 `account_id`
- `account_id` 创建后锁死，不允许修改

### 2. 平台身份守卫：`bound_unb`

- `bound_unb` 是平台身份绑定字段
- 首次成功登录/导入/扫码完成后，将拿到的 `unb` 绑定到该 `account_id`
- 后续任何登录方式再次拿到 `unb` 时，必须执行一致性校验：
  - 未绑定：允许绑定
  - 已绑定且一致：允许通过
  - 已绑定但不一致：直接拦截并回滚，禁止覆盖

### 3. 浏览器 runtime

每个 `account_id` 对应一个账号级 runtime state，建议至少包含：

- `account_id`
- `profile_dir`
- `process`
- `playwright`
- `browser`
- `context`
- `alive`
- `invalid_reason`
- `lock`
- `ref_count`
- `last_used_at`
- `pinned_reason`
- `generation`

### 4. 浏览器画像目录

- 目录规则固定为：`browser_data/user_<account_id>`
- runtime 重建时可以重建进程，但必须继续复用同一个 `profile_dir`
- `unb`、扫码 `session_id`、临时 cookie 值都不得参与 profile 路径计算

### 5. 操作类型：`purpose`

所有浏览器调用都必须带 `purpose`，至少区分为：

- 只读：`item_search`、`order_detail_fetch`
- 写状态：`password_login`、`qr_login`、`manual_cookie_import`、`cookie_refresh`、`verification_recovery`

`purpose` 用于日志、锁策略、超时策略、失败处理和后续扩展。

## 架构设计

### 1. 账号级浏览器 runtime 管理器

新增统一的账号级 runtime 管理器（命名可为 `AccountBrowserRuntimeManager`），作为所有浏览器相关业务的唯一入口。

建议提供以下接口：

- `acquire_runtime(account_id, purpose, exclusive=False)`
- `release_runtime(account_id, purpose)`
- `get_fresh_page(account_id, purpose)`
- `invalidate_runtime(account_id, reason)`

可为后续预留：

- `handoff_runtime(from_account_id, to_account_id)`，但本设计不依赖该能力；由于扫码也要求先定 `account_id`，正常情况下不会发生 runtime 身份迁移

该管理器职责仅限于：

- runtime 懒启动
- profile_dir 绑定
- 进程 / browser / context 生命周期管理
- 并发锁
- 失效标记与重建
- 空闲回收
- 基础日志上下文

业务模块不得再自行 `launch_browser_*`、`connect_over_cdp()`、`new_context()`。

### 2. 登录链路统一

#### 账号密码登录

- 接口必须显式接收 `account_id`
- 流程固定为：
  1. 校验账号归属
  2. `acquire_runtime(account_id, purpose='password_login', exclusive=True)`
  3. 在该 runtime 内执行登录、风控、cookie 获取、token 预热
  4. 校验 / 绑定 `bound_unb`
  5. 原子提交 cookie、任务状态与账号运行状态
  6. `release_runtime(...)`

#### 扫码登录

扫码登录也必须在开始前先绑定 `account_id`，分为两种模式：

- **已有账号扫码**
  - 前端先选中账号，带 `account_id` 发起扫码
  - 二维码展示、扫码确认、cookie 落盘、token 预热全部在该账号 runtime 内完成
  - 如果拿到的 `unb` 与已绑定 `bound_unb` 不一致，直接拦截并回滚

- **新增账号扫码**
  - 先创建一个新的 `account_id`
  - 账号状态可先为 `pending_bind`
  - 再以该 `account_id` 发起扫码
  - 扫码成功后把 `unb` 绑定到该账号

禁止“先扫码成功，再按 `unb` 匹配或创建账号”的旧式后置归属流程。

#### 手动 Cookie 导入

手动 Cookie 导入分两段：

1. **HTTP 预检**
   - 优先用纯 HTTP 校验 cookie 是否直接有效、是否缺关键字段、是否需要浏览器补态
2. **按需进入浏览器**
   - 只有在需要补 token、需要验证页、需要浏览器实际落态时，才进入该 `account_id` runtime

进入浏览器时同样必须：

- 显式 `account_id`
- 独占 runtime
- 完成 `bound_unb` 校验
- 成功后统一提交

### 3. 旁路浏览器能力统一

所有浏览器旁路能力必须显式带 `account_id`，并统一走 runtime 管理器。

#### 商品搜索

当前 `reply_server.py` 的 `/items/search` 与 `/items/search_multiple` 未显式要求账号，`utils/item_search.py` 还会拿“第一个有效 cookie”并使用共享缓存目录。

改造要求：

- 搜索 API 请求体必须显式包含 `account_id`
- `search_xianyu_items(...)`、`search_multiple_pages_xianyu(...)` 等内部函数都改为以 `account_id` 为首要入参
- 搜索逻辑只允许：
  - `acquire_runtime(account_id, purpose='item_search')`
  - `get_fresh_page(account_id, purpose='item_search')`
  - 用完关闭 page、保留 runtime

必须移除以下行为：

- 扫全局 cookie 选“第一个有效 cookie”
- 使用共享浏览器缓存目录，如 `tempfile.gettempdir()/xianyu_browser_cache`
- 在搜索模块内部自行创建持久化 context

#### 订单详情抓取

当前 `utils/order_detail_fetcher.py` 仍以 `cookie_string` 驱动浏览器详情页抓取，并直接 `launch_browser_async()` / `new_context()`。

改造要求：

- 统一改为 `fetch_order_detail_simple(order_id, account_id, ...)`
- `OrderDetailFetcher` 以 `account_id` 而不是原始 `cookie_string` 作为主身份输入
- 先查数据库缓存；只有缓存不足时才进入该账号 runtime
- 浏览器抓取只允许通过 runtime 管理器获取 fresh page

必须移除以下行为：

- 以原始 `cookie_string` 驱动账号身份
- 在详情抓取器内部直接启动独立浏览器

#### 手动刷新 / 验证恢复 / Token 恢复

这些属于写状态操作，要求更严格：

- 必须显式带 `account_id`
- 必须以独占方式进入 runtime
- 必须在一个 runtime 内完成状态修复、验证、回写
- 失败时必须按统一回滚策略处理

### 4. API 边界

浏览器相关 API 统一要求：

- 无 `account_id`：拒绝请求
- `account_id` 不属于当前用户：返回 `403`
- 账号未登录 / 未绑定完成：返回明确业务错误
- 当前账号 runtime 被独占操作占用：返回冲突错误

推荐的语义约束：

- `400`：参数缺失或非法
- `403`：账号不属于当前用户
- `409`：账号当前有独占浏览器操作
- `428` 或业务码：账号尚未具备可用登录态
- `410` 或业务码：runtime 已失效，需要重建或重新登录

API 层职责仅限于：

- 验参
- 用户与账号归属校验
- 调用业务服务

业务服务层负责：

- 调用 runtime 管理器
- 执行业务页面流程
- 处理成功、失败、回滚

runtime 管理器负责：

- 浏览器生命周期
- 锁
- 失效判定
- 回收
- 基础日志

## 并发、fallback 与生命周期设计

### 1. 并发策略

一期采用最保守且最稳定的策略：

- **同一 `account_id` 同一时刻只允许一个浏览器操作运行**
- 不区分只读/写状态，一期默认全串行

原因：

- 当前项目更需要稳定和可排障，而不是同账号浏览器吞吐
- 同账号多页并发会显著增加风控、token、storage 竞争风险

虽然一期全串行，但内部仍保留“只读 / 写状态”分类，为后续精细化放开并发做准备。

### 2. fallback 规则

fallback 必须遵守一条铁律：

> 只能重建同账号 runtime，不能更换身份。

允许的 fallback 顺序：

1. **page 级恢复**
   - 关闭当前 page
   - 在同一个 runtime 内重新开 fresh page
   - 对只读操作或无副作用步骤可重试一次
2. **runtime 级软重建**
   - 当 page/context/browser 已关闭、CDP 断连、进程退出或 runtime 明显脏掉时
   - 关闭旧 runtime
   - 使用同一个 `account_id` 与同一个 `profile_dir` 重新拉起 runtime
3. **账号级失败上抛**
   - 若重建后仍失败，或出现身份冲突，直接标记 runtime invalid，并要求人工重新登录或重新验证

明确禁止的伪兜底：

- 切到临时匿名 context
- 切到共享缓存目录
- 扫全局 cookie 顶上
- 从 `account_id=A` 悄悄切换成 `account_id=B`
- 新建一套临时 profile 假装恢复成功

### 3. runtime 失效判定

以下情况任一命中，runtime 直接判死：

- browser 进程退出
- CDP 连接断开
- persistent context 被关闭
- profile_dir 被占用或损坏，无法继续工作
- 新拿到的 `unb` 与 `bound_unb` 不一致
- 连续验证恢复失败，无法恢复到稳定登录态
- 关键 cookie / token 校验失败且受控重试后仍失败

判死后应执行：

- `alive = False`
- 记录 `invalid_reason`
- 清理 page/context/browser 引用
- 唤醒等待中的请求，并返回 runtime 已失效的明确错误

### 4. 提交与回滚

写状态操作必须采用“两阶段”处理：

1. **运行时验证阶段**
   - 在 runtime 内完成登录、验证、cookie 获取、token 预热、`unb` 校验
2. **原子提交阶段**
   - 全部通过后，统一更新数据库 cookie、`bound_unb`、任务状态、内存态管理器

若任一步失败：

- 不提交半成品 cookie
- 不覆盖旧 `bound_unb`
- 不切换到新账号运行态
- 如为新增账号扫码未完成，应删除临时账号或恢复 `pending_bind`
- 如为已有账号刷新失败，应恢复旧 cookie / 旧任务状态

### 5. 空闲回收与保温

runtime 生命周期建议如下：

- **懒启动**：无请求时不提前起 runtime
- **短期保温**：刚完成登录、扫码、导入、验证恢复后，可保温 2~5 分钟
- **空闲回收**：最后一次使用后 10~15 分钟无请求，则关闭 runtime 进程
- **画像保留**：关闭 runtime 只关活进程，不删除 `profile_dir`

这样既能控制资源占用，又不会丢失账号画像。

## 数据流

### 1. 账号密码登录

1. API 接收 `account_id`
2. 校验当前用户对该账号的访问权限
3. runtime 管理器获取或拉起该账号 runtime
4. 登录、风控、cookie 获取、token 预热
5. 校验 / 绑定 `bound_unb`
6. 原子提交 cookie 与账号运行状态
7. release runtime

### 2. 已有账号扫码登录

1. 前端选中既有 `account_id`
2. 用该 `account_id` 发起二维码会话
3. 二维码展示、扫码确认、Cookie 获取都在同一个 runtime 内完成
4. 获取真实 cookie 与 `unb`
5. 校验 `bound_unb`
6. 原子提交并恢复账号任务

### 3. 新增账号扫码登录

1. 先创建新的 `account_id`
2. 账号进入 `pending_bind`
3. 使用该 `account_id` 发起扫码
4. 扫码成功后绑定 `unb`
5. 提交 cookie 与账号状态

### 4. 商品搜索 / 订单详情

1. API 显式接收 `account_id`
2. 校验账号归属与登录态
3. runtime 管理器为该账号提供 fresh page
4. 执行只读操作
5. 关闭 fresh page，保留 runtime

## 观测与排障

每次 runtime 操作的日志建议统一带上：

- `account_id`
- `purpose`
- `operation_id`
- `runtime_generation`
- `profile_dir`
- `exclusive`
- `acquire/release`
- `retry_stage`（`page` / `runtime`）
- `invalid_reason`

这样可以快速定位：

- 哪个账号在操作
- 用的是哪一代 runtime
- 是 page 级问题还是 runtime 级问题
- 是否发生了 `bound_unb` 冲突

## 测试策略

### 1. 设计级验证

- 同一 `account_id` 的账密登录、扫码登录、手动 Cookie 导入全部落到同一 `profile_dir`
- 旁路能力未传 `account_id` 时直接失败
- 搜索与订单详情不再使用共享缓存目录，不再扫全局 cookie
- 同账号写状态操作失败后能恢复旧状态，不留下半成品

### 2. 单元 / 集成测试重点

- runtime 管理器：
  - 懒启动
  - acquire/release 计数
  - runtime 失效后重建
  - 空闲回收
- 登录链路：
  - 账密登录 `bound_unb` 首绑与冲突
  - 已有账号扫码与新增账号扫码
  - 手动 Cookie 导入的“纯 HTTP 预检 / 浏览器补态”分支
- 旁路链路：
  - 搜索必须显式传 `account_id`
  - 订单详情必须显式传 `account_id`
  - 同账号搜索与写状态操作并发时的锁行为
- 错误路径：
  - browser/context/page 被关闭
  - CDP 断连
  - profile_dir 损坏
  - `unb` 不一致

### 3. 手工验证重点

- 用同一账号分别走账密登录、扫码登录、手动 Cookie 导入，确认浏览器画像一致
- 搜索 A 账号商品时，不会串用 B 账号登录态
- 刷新 / 验证恢复失败时，旧账号状态仍可保留
- runtime 空闲回收后再次进入仍能复用同一 `profile_dir`

## 受影响模块

本设计后续实施将主要影响以下模块：

- `reply_server.py`
- `utils/browser_provider.py`
- `utils/qr_login.py`
- `utils/xianyu_slider_stealth.py`
- `utils/item_search.py`
- `utils/order_detail_fetcher.py`
- `XianyuAutoAsync.py`
- 可能涉及 `cookie_manager.py`、`db_manager.py` 的少量接口适配

## 本轮不做

1. 不在本轮引入“每账号多活浏览器”或浏览器池调度
2. 不在本轮为了兼容旧接口而保留“自动猜账号”的旁路
3. 不在本轮允许 `account_id` 迁移或重命名
4. 不在本轮把 `unb` 提升为系统内部主键

## 结论

本设计将账号体系与浏览器体系统一到同一个稳定主键 `account_id` 上，并以账号级 runtime 管理器作为所有浏览器行为的唯一收口。这样可以同时解决以下核心问题：

- 不同登录方式无法再落到不同浏览器身份
- 搜索、订单详情等旁路能力无法再串用其他账号状态
- fallback 不再通过偷偷换身份“伪成功”
- 并发、回滚、失效、回收都能基于同一套模型处理

后续实施应以“先统一入口、再替换旧链路、最后清理 fallback”为基本顺序推进。
