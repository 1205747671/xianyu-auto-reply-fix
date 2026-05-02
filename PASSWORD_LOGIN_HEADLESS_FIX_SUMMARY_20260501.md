# 密码登录无头链路修复总结（2026-05-01 / 2026-05-02）

## 结论

账号密码登录这条链路已经从“无头直接白屏/进不去表单”修到可以：

1. 无头浏览器正常打开闲鱼 IM 登录页；
2. 正常进入账号密码登录表单；
3. 自动触发并通过阿里滑块；
4. 正确进入后续二维码/人脸验证流程；
5. 项目内保留验证截图/链接，供前端或人工继续处理。

当前这个测试账号（已脱敏）在滑块后进入的是 **二维码验证**，不是账密错误，也不是滑块失败。

---

## 这次定位到的两个核心问题

### 1) 代码错误：`sync_playwright` 未定义

`login_with_password_playwright()` 里直接调用了：

```python
playwright = sync_playwright().start()
```

但文件实际导入的是：

```python
from playwright.sync_api import sync_playwright as playwright_sync_playwright
```

所以密码登录链路一跑就会先炸：

```text
name 'sync_playwright' is not defined
```

已改为统一复用现有的 `_get_sync_playwright_factory()`。

---

### 2) 策略错误：无头 `full stealth` 会把登录页前端干白屏

实测发现：

- `playwright + headless + full stealth`
  - `/im` 页面的 `#ice-container` 不渲染
  - 页面异常：`a.addEventListener is not a function`
  - 导致“未找到登录表单”

- `playwright + headless + lite stealth`
  - 登录页正常渲染
  - 可进入账号密码表单
  - 可触发并通过滑块

所以这次不是“轨迹不对”，而是 **full 级反检测脚本把密码登录页的前端事件系统改坏了**。

---

## 已做代码修改

### 1. `utils/xianyu_slider_stealth.py`

- 修复 `sync_playwright` 调用错误；
- 密码登录链路启动浏览器时复用：
  - 项目内 Playwright 浏览器
  - 自动化后端选择逻辑
  - 代理配置
  - `channel / executable_path`
- 密码登录链路支持注入：
  - `initial_cookies`
  - 数据库旧 Cookie
- 为密码登录链路增加**场景化 stealth 策略**：
  - 在 `stealth_mode=auto` 且 `headless=True` 时，
  - `login_with_password_playwright()` 默认改用 `lite`
  - 目的是避免登录页白屏
- 增加验证等待参数：
  - `XY_VERIFICATION_WAIT_TIMEOUT`
- 增加验证截图保留参数：
  - `XY_KEEP_VERIFICATION_SCREENSHOT=1`

### 2. `XianyuAutoAsync.py`

自动密码登录刷新时创建 `XianyuSliderStealth(...)` 补齐：

- `initial_cookies=self.cookies_str`
- `proxy=self.proxy_config`

### 3. `reply_server.py`

手动账号密码登录接口创建 `XianyuSliderStealth(...)` 时补齐：

- 从数据库取旧 Cookie 注入 `initial_cookies`
- 从数据库取账号代理配置注入 `proxy`

### 4. `debug_manual_password_login.py`

新增单次手动密码登录调试脚本，特点：

- 不走后台无限重试逻辑
- 单次真实浏览器验证
- 支持无头 / 有头
- 支持自动化后端切换
- 支持验证超时调小
- 支持保留二维码/人脸验证截图
- 会打印验证截图路径 / 验证链接

---

## 实测结果

### A. 默认无头自动策略（`auto`）

命令：

```powershell
.\.venv\Scripts\python.exe -u debug_manual_password_login.py --account-id manual_password_default_flow --account 131****6772 --password <已脱敏> --headless --max-retries 1 --verification-wait-timeout 20 --keep-verification-screenshot
```

关键结果：

- 登录页标题正常：`聊天_闲鱼`
- 找到登录表单
- 自动点击“密码登录”
- 自动输入账号密码
- 自动勾选协议并提交
- 自动检测并通过滑块
- 滑块后进入 `qr_verify`
- 成功保存验证截图

说明：

> 默认无头密码登录链路已经被修到“正确走到后续人工验证节点”。

当前账号后续是否能直接登录成功，取决于该账号是否被要求继续扫二维码 / 做人脸验证。

---

## 当前这个账号的实际状态

实测这个账号在：

```text
账号密码提交 -> 滑块通过 -> 二维码验证
```

所以当前阻塞点已经不是：

- 登录页白屏
- 找不到表单
- 无头过不了滑块

而是：

- 账号本身被风控要求继续做二维码/人脸验证

这属于账号验证阶段，不是脚本前半段失败。

---

## 调试建议

### 1. 手动单次调试

```powershell
.\.venv\Scripts\python.exe -u debug_manual_password_login.py --account-id debug1 --account 131****6772 --password <已脱敏> --headless --max-retries 1 --verification-wait-timeout 20 --keep-verification-screenshot
```

### 2. 看验证截图

日志会打印类似路径：

```text
static/uploads/images/face_verify_<account_id>_<timestamp>.jpg
```

### 3. 如果要长时间等待人工扫码/人脸

不要传很短的 `--verification-wait-timeout`，或者直接让项目正式前端接口去接管会话状态轮询。

---

## 现在项目里的默认行为

### Cookie 滑块链路

- 默认：`playwright + headless + full stealth`
- 这条链路实测可直接拿到 `x5sec`

### 密码登录链路

- 默认：`playwright + headless + auto`
- 但在 `login_with_password_playwright()` 内部会自动降为 `lite stealth`
- 这是为了避免登录页前端白屏

也就是说：

**同一个项目里，Cookie 滑块链路和密码登录链路现在是按不同页面特性分开处理的。**

这才是正经做法，不是拿一套脚本到处硬抹。

---

## 2026-05-02 补充修复：账密无头正式链最终打通

### 这次真正卡住的问题

前一版虽然已经做到：

- 登录页无头不白屏
- 能进账号密码表单
- 能触发滑块

但账密正式链还是会卡在：

```text
验证失败，点击框体重试(error:NXY2)
```

最坑的地方就在这：

- 登录页如果直接上 `full stealth`，表单会失踪
- 所以前一版只能让**整个账密链路**都先跑 `lite`
- 结果登录页是活了，**滑块页的运行时指纹还他妈是 lite**
- 轨迹看着像那么回事，风控照样把你按地上摩擦

所以根因不是单纯轨迹，而是：

> **账密链需要“登录页 lite，滑块页 full”，不能一锅炖。**

---

## 这次新增的关键修复

### 1）账密登录页继续 `lite`，但滑块页切到 `full runtime stealth`

文件：

- `utils/xianyu_slider_stealth.py`

新增逻辑：

- `_harden_password_slider_runtime(...)`

行为：

- 登录页还是维持 `lite`
- 一旦检测到账密链里的滑块 frame：
  - 对当前 `page/frame` **直接补注入 full stealth**
  - 同时把后续文档也补上 full `init_script`

这样就把两个阶段拆开了：

- **登录表单阶段**：优先稳定渲染
- **滑块验证阶段**：优先过风控

这才是像人干的修法，之前那种一套脚本抹全场，纯纯给自己上刑。

---

### 2）真正启用无头网络层 UA / Client Hints 伪装

之前代码里虽然已经有：

- `_apply_headless_network_fingerprint(...)`

但实际账密正式链里**没真用上**。

这次改成：

- `new_page()` 后立即执行网络层伪装
- 滑块阶段再次补强

核心点：

- `Network.setUserAgentOverride`
- `userAgentMetadata`
- `sec-ch-ua`
- `sec-ch-ua-platform`
- `navigator.userAgentData`

这块不补，滑块页 runtime 和 network 两层指纹对不上，阿里那套风控不是吃素的。

---

### 3）账密学习样本白名单化

之前全局 fallback 会把同画像成功样本一股脑掺进来。

这次改成：

- 账密场景优先只吃：
  - `password`
  - `pwd`
 相关成功样本
- 自动排除：
  - `cookie`
  - `import_user_cookie`
  - `ui_cookie`

同时把账密场景距离容忍收紧为更保守的范围，避免拿错样本把学习方向带偏。

---

### 4）补了账密第 4 次重试的模板化兜底

虽然这次正式跑第 1 次就过了，但兜底也顺手修了：

- 第 4 次不再放飞自我搞 `15%` 超调
- 改成账密成功样本附近的模板范围回放

这块主要是为了防止以后再回到“前 3 次像人，第 4 次像癫子”的狗状态。

---

## 本次真实验证结果

### 正式链实测账号

- 账号ID：`formal_password_headless_fix2_20260502`
- 登录账号：`131****6772`
- 会话ID：`3lojgYwrG9_W2Xjn_SqB1Q`
- 日期：**2026-05-02**

### 真实结果

正式无头账密链已经做到：

1. 使用**项目内自动下载 Chromium**
2. 无头打开登录页
3. 正常输入账号密码
4. **第 1 次滑块直接通过**
5. 继续进入后续 **二维码验证**

也就是说现在正式链已经是：

```text
账号密码 -> 无头滑块成功 -> 二维码/人脸后续验证
```

这就叫链路打通，不是嘴炮打通。

---

## 关键证据

### 1. 已确认使用项目内浏览器

日志：

- `realtime.log:21029`

关键内容：

```text
复用项目内 Playwright 浏览器:
C:\Users\12057\Desktop\xianyu\xianyu-auto-reply-fix\.playwright-browsers\chromium-1217\chrome-win64\chrome.exe
```

---

### 2. 滑块阶段已切到 full 补强

日志：

- `realtime.log:21214`
- `realtime.log:21215`
- `realtime.log:21216`

关键内容：

```text
已应用无头浏览器 UA/Client-Hints 网络层伪装
账密滑块场景已切换到 full runtime stealth 补强
已对账密滑块当前文档补注入 full stealth: target, page
```

---

### 3. 滑块已真实通过

日志：

- `realtime.log:21405`

关键内容：

```text
✅ 滑块验证成功! (第1次尝试)
```

成功记录：

- `trajectory_history/formal_password_headless_fix2_20260502_success.json`

关键参数：

- 距离：`312.2478450814047`
- 步数：`37`
- 超调：`1.0804376463900942`
- 延迟：`10.83ms`
- 曲线：`^1.894`
- 画像：`win_chrome_147_1600x900`
- `headless=true`

---

### 4. 滑块后进入二维码验证

日志：

- `realtime.log:21462`

关键内容：

```text
⚠️ 需要二维码验证
```

验证截图：

- `static/uploads/images/face_verify_formal_password_headless_fix2_20260502_20260502_013329.jpg`

这说明现在失败点已经不在滑块，而在账号后续验证要求本身。

---

## 当前最终结论

到 **2026-05-02** 为止，账密无头正式链已经不是“有概率过”，而是已经真实验证过：

- **项目内自动下载浏览器**：是
- **无头模式**：是
- **正式 `/password-login` 链路**：是
- **真实过滑块**：是
- **进入二维码/人脸后续验证**：是

所以当前这条链路的正确认知应该改成：

> **账密无头登录的滑块问题已解决。**
>
> 现在如果账号继续卡住，优先看二维码 / 人脸 / 账号风控，不要再回头拿滑块当背锅侠。

---

## 2026-05-02 二次正式回归：`/password-login` 继续验证

### 本轮正式验证

- 账号ID：`formal_password_headless_verify_20260502`
- 登录账号：`131****6772`
- 会话ID：`Wozb4r_avvw03-FfcEkizA`
- 模式：**无头**
- 接口：`POST /password-login`
- 结果：`verification_required`

### 关键证据

- 项目内浏览器：
  - `realtime.log:27337`
- 登录页先走 `lite`：
  - `realtime.log:27343`
- 无头网络层伪装已生效：
  - `realtime.log:27408`
- 账密滑块阶段切到 full runtime stealth：
  - `realtime.log:27409`
  - `realtime.log:27410`
- 第 1 次滑块成功：
  - `realtime.log:27624`
  - `realtime.log:27654`
- 随后识别到二维码验证：
  - `realtime.log:27696`
  - `realtime.log:27698`
  - `realtime.log:27705`
  - `realtime.log:27706`

验证截图：

- `static/uploads/images/face_verify_formal_password_headless_verify_20260502_20260502_020115.jpg`

### 这轮要点

这次结果再次坐实了一件事：

> **账密无头链当前卡点不是滑块，而是滑块后的二维码/身份验证。**

也就是说，现在要盯的是后续验证承接，不要再搁那儿对着滑块瞎拱。

---

## 2026-05-02 同轮代码清理

1. **去掉滑块失败后偷偷切有头**
   - 删除 `_try_password_login_refresh(..., force_show_browser=True)` 这条暗门
   - 防止正式无头链被后台代码自己带歪

2. **账密成功 Cookie 先保护性合并再交接**
   - 避免浏览器快照漏掉 `havana_lgc2_77` 一类关键字段
   - 不让一份不完整 Cookie 把原会话里还能用的字段冲没

3. **账密新 Cookie 先做 token 预检**
   - 新增 `preflight_token_after_password_login()`
   - 先确认服务端已接受，再交给新实例接管

4. **关键字段检查统一 `cna` 口径**
   - `cna` 改为观察字段，不再把它当硬性必需
   - 但日志和诊断里照样会打出来
