# CloakBrowser Provider Replacement Design

## 背景

当前仓库的浏览器自动化已经深度绑定 `playwright`，并且还残留一条 `patchright` 分支：

- 登录 / 滑块主链集中在 `utils/xianyu_slider_stealth.py`、`XianyuAutoAsync.py`
- 搜索、订单详情、二维码验证等旁路流程也各自直接 `import playwright`
- `Start.py`、`Dockerfile*`、`README.md` 里也写死了 Playwright 安装与缓存逻辑

这套结构的问题不只是“浏览器版本不一致”，而是整个项目把 provider 细节散在了很多文件里。要换指纹浏览器，不能只改一个登录入口糊弄过去，不然早晚别的链路继续漏出原来的 Playwright 痕迹。

用户本轮确认的目标是：

1. 用 `CloakBrowser` 替代现有浏览器 provider
2. 删除旧的 `playwright` / `patchright` 直接接入和 fallback 思路
3. 所有浏览器操作都切到新 provider
4. 先完成本地接入，不先做真实账号密码实测

## 目标

- 让项目中的**所有活跃浏览器链路**统一走 `CloakBrowser`
- 删除 `patchright` 相关代码、依赖和环境开关
- 删除项目里对 `playwright` Python 包的直接业务依赖
- 保留现有 `Page / Browser / Context` 风格的调用语义，尽量不重写业务流程本身
- 保留账号级 profile 目录策略：`browser_data/user_<account_id>`
- 先完成本地可集成、可静态验证、可跑单测的状态，再进入真人登录测试

## 非目标

- 这轮不直接解决 `token_refresh` 风控是否通过
- 这轮不先上 Linux 远端或 Docker 实测真实账号
- 不顺手重构无关业务模块
- 不保留“多 provider 并存 + 自动切换”的历史兼容包袱

## 方案对比

### 方案 A：全仓直接把 `playwright` import 改成 `cloakbrowser`

优点：

- 表面上最快
- 不需要先抽象 provider 层

缺点：

- provider 细节会继续散落在各个模块
- 一旦 CloakBrowser 的 Python API 和 Playwright 不是 100% 同名，改动会在很多文件里重复开花
- 后续调试、测试替身、Docker 安装逻辑都会继续分裂

结论：不推荐单独采用。

### 方案 B：先收口 provider，再批量切换调用点

做法：

- 新增一个项目内 provider 适配层，把浏览器启动、关闭、安装、代理、持久 profile、类型别名都收口
- 业务模块改为依赖这个适配层，而不是直接 import Playwright
- 同时把项目里带 `playwright` 语义的方法名和提示文案逐步改成中性命名

优点：

- 风险集中，后续改 provider 不会再满地开炮
- 便于统一处理本地 / Docker / 持久 profile / 代理
- 单测可以用一个假的 provider runtime 顶掉，不需要每个文件各自 monkeypatch 一套 Playwright

缺点：

- 前期要多做一层抽象
- 第一版 diff 不会特别小

结论：**推荐方案**。

### 方案 C：引入外部 browser manager / CDP attach 服务

优点：

- 看起来“更专业”，能把浏览器进程放到外部

缺点：

- 额外引入服务编排和生命周期管理
- 不符合“先本地接入、先不做远端部署”的目标
- 当前仓库现有逻辑大量依赖本地直接起 browser/context/page，强行改成外部连接会把面摊太大

结论：本轮不选。

## 最终设计

### 1. 新增统一 provider 适配层

新增一个浏览器 provider 模块（建议 `utils/browser_provider.py`），负责：

- 统一导出 sync / async 启动入口
- 统一封装 provider 安装与可用性检查
- 统一封装代理与下载代理注入
- 统一解析持久 profile 路径
- 统一管理 provider 相关环境变量
- 给测试提供统一 fake runtime 注入点

这个模块的职责是把“项目想要一个浏览器上下文”翻译成“CloakBrowser 具体该怎么起”。业务模块以后不该再到处自己碰 provider 细节。

### 2. 项目内统一改成 provider-neutral 调用

以下现有直接依赖 Playwright 的活跃模块，统一切到 provider 适配层：

- `utils/xianyu_slider_stealth.py`
- `XianyuAutoAsync.py`
- `reply_server.py`
- `debug_manual_password_login.py`
- `debug_manual_cookie_slider.py`
- `utils/item_search.py`
- `utils/order_detail_fetcher.py`
- `utils/qr_login.py`
- `utils/captcha_remote_control.py`
- `Start.py`

配套还要同步改：

- `requirements.txt`
- `Dockerfile`
- `Dockerfile-cn`
- `docker-compose.yml`
- `docker-compose-cn.yml`
- `README.md`
- `PASSWORD_LOGIN_VERIFICATION_FLOW_FIX_SUMMARY_20260504.md`
- `tests/` 下与浏览器 provider 强绑定的用例

### 3. 删除旧 provider 分支，不保留 legacy fallback

下面这些东西要一起下线，不再留着恶心人：

- `patchright` 依赖与 import
- `XY_SLIDER_AUTOMATION_BACKEND`
- `_get_sync_playwright_factory()` 这类多后端选择器
- “auto / patchright / playwright” 选择参数
- 直接提示用户执行 `playwright install chromium` 的文案

保留的 only one path 是：**CloakBrowser 作为唯一 provider**。

需要说明的边界是：

- 不保留“旧 provider 失败再切另一个 provider”的 fallback
- 但在**同一个 provider 内部**，如果已有逻辑区分 persistent context / temporary context，这种业务级恢复分支可以保留，不算多 provider fallback

### 4. 保留现有账号级 profile 目录策略

`browser_data/user_<account_id>` 继续保留，原因很简单：

- 这套目录命名已经被 `token_refresh`、登录恢复链、账号级隔离逻辑依赖
- 直接推翻目录策略只会把问题从“浏览器 provider 替换”扩散成“状态迁移 + 风控状态丢失”

所以设计上不改目录策略，只改底层 provider 的启动方式，让 CloakBrowser 对接同一套账号 profile 目录。

### 5. 统一改名，清掉误导性的 Playwright 语义

当前项目里不少方法名已经把旧 provider 写死了，比如：

- `_start_playwright_safe`
- `login_with_password_playwright`

既然这次是“去大改”，那就别再顶着旧名装新瓶。设计上要改成中性命名，例如：

- `_start_browser_runtime_safe`
- `login_with_password_browser`

对应调用方、日志、CLI 参数、测试名也一起改。这样后面再看代码，不至于像披着羊皮的拖拉机，读一眼就膈应。

### 6. 清理已无引用的旧文件

当前仓库里 `utils/slider_patch.py` 已经没有活跃引用。如果实现阶段复核后仍确认无运行期依赖，就直接删掉，不再保留这类历史尸体。

## 运行与部署设计

### 本地阶段

- 本地先完成依赖安装、导入路径、provider 启动、静态校验、单测替身适配
- 不在这一阶段使用真实账号密码做验收

### Docker / Linux 阶段

- 保持 `docker-compose` 启动方式不变
- build 阶段按照用户给的约束使用：
  - Linux: `export HTTPS_PROXY=http://192.168.31.188:10809`
- 本地下载或拉依赖时允许使用：
  - `http://127.0.0.1:1081`

实现时只把这些代理接入到 provider 安装 / 下载链路，不改业务请求代理逻辑。

## 外部依赖的实现前校验项

在真正写代码前，必须先用官方仓库或官方文档核实下面几件事：

1. `cloakbrowser` 的 Python 安装方式
2. 它的 sync / async API 真实 import 路径
3. 是否支持 persistent context
4. 如果它对外是“Playwright-compatible”，兼容边界到底到哪
5. Docker / Linux 下是否需要单独安装浏览器资产，还是由包自身处理

这里不能脑补。README 里哪怕写得像 Playwright，也得确认 Python 侧到底是同名 API 还是包装层 API。否则一顿猛改，最后 import 都对不上，那就不是改代码，是给自己上供。

## 测试与验证策略

这轮先不做真人登录验证，所以验收分两层：

### 第一层：静态与单测

- provider 适配层可导入
- 现有依赖浏览器的模块可成功导入
- 现有测试里对 fake Playwright 的替身，改成 fake provider runtime 后仍能验证关键分支
- 至少覆盖：
  - 登录入口调用链
  - `token_refresh` 风控接管链
  - 搜索 / 订单详情 / 二维码验证这些旁路链

### 第二层：本地手工 smoke（不碰真实账号）

- 调试脚本能走到 provider 初始化阶段
- 缺少浏览器资产、provider 不可用、profile 被占用等错误提示要明确
- Docker 构建链路中的 provider 安装命令可被正确触发

### 第三层：后续单独会话再做真人验证

等你明确说“开始测试”，再做：

1. 本地非 Docker 的账号密码登录测试
2. 通过后再同步到 Linux / Docker 环境

## 风险

- `utils/xianyu_slider_stealth.py` 是重灾区，改动量最大，容易牵一发动全身
- 现有很多测试名字和 fake 对象都带 `playwright` 语义，替换时需要一起重命名，不然测试会越来越假
- CloakBrowser 如果只是“兼容大部分 Playwright”，那类型提示、事件回调、持久 context 参数名可能会出现边角差异
- Docker 里的浏览器资产安装方式可能和本地不一样，这部分必须放到 provider 适配层收口，不能再散在 `Start.py` 和各业务模块里

## 验收标准

- 项目代码中不再保留对 `patchright` 的运行期依赖
- 项目业务模块不再直接依赖 `playwright` Python 包
- 所有活跃浏览器链路统一通过 `CloakBrowser` provider 适配层启动
- 账号级 profile 目录策略保持不变
- 本地可完成静态校验与针对性单测
- 文档、Docker、调试脚本、启动脚本与新 provider 保持一致

