# 无头滑块修复总结

日期：2026-05-01

## 背景

这个项目在“手动填入 Cookie 后触发阿里滑块”的链路里，存在下面的问题：

- 有头浏览器偶尔能过
- 无头浏览器成功率很差
- 原后台链路会不断重试，不利于定位
- 之前实现更像是在反复调轨迹，但实际问题不只是“没滑到位”，还包含浏览器环境/指纹稳定性

本次目标是：

- 保持真实浏览器运行
- 主攻无头模式
- 不依赖本机已安装浏览器
- 项目内自动下载/复用 Playwright 浏览器
- 修改项目默认代码，让无头链路尽量稳定通过

---

## 本次最终结论

这次实测后的结论很明确：

1. **问题不只是轨迹**
   - 失败时经常是页面内直接出现：
     - `验证失败，点击框体重试(error:xxxxxx)`
   - 说明很多时候是本地风控校验/浏览器环境识别不过，不是单纯距离不对

2. **Patchright 不是这题当前最稳的解**
   - `patchright + headless` 有成功样本，但波动很大
   - 项目里原本那套重型 `init_script` 和 Patchright 的行为可能互相打架

3. **当前最稳的默认方案是**
   - **`playwright + headless + full stealth`**

4. **项目默认代码已经改成走这条成功链路**
   - 现在无头默认后端不再优先 `patchright`
   - 默认改为 `playwright`

---

## 代码改动总览

### 1）`utils/xianyu_slider_stealth.py`

这是本次修改的核心文件。

#### 已做修改

- 增加项目内 Playwright 浏览器自动下载/复用逻辑
  - 浏览器缓存目录：`.playwright-browsers/`
  - 默认下载代理：`http://127.0.0.1:1081`

- 增加后端选择逻辑
  - 支持：
    - `playwright`
    - `patchright`
  - **默认后端改为 `playwright`**
  - 如果想强制切换，仍可通过环境变量：
    - `XY_SLIDER_AUTOMATION_BACKEND=playwright`
    - `XY_SLIDER_AUTOMATION_BACKEND=patchright`

- 增加 `stealth_mode`
  - 支持：
    - `off`
    - `lite`
    - `full`
  - 默认逻辑：
    - `patchright + headless` -> 默认 `off`
    - 其他情况 -> 默认 `full`

- 新增无头稳定轨迹策略
  - 针对成功样本收敛出一套更接近真实成功分布的参数
  - 主要约束：
    - 步数
    - 超调比例
    - base delay
    - curve
    - hover 行为
    - server judge wait

- 无头模式默认跳过 warmup
  - 避免先访问其它页面把风控状态搞脏
  - 如需恢复可设置：
    - `XY_SLIDER_HEADLESS_WARMUP=1`

- 失败快照增强
  - 在调试快照里新增运行时信息：
    - `navigator.webdriver`
    - `navigator.userAgent`
    - `navigator.languages`
    - `navigator.userAgentData.brands`
    - `AWSC`
    - `__awsc_et__`
    - `nc`
    - DOM 错误文本
    - 当前后端
    - 当前 stealth 模式

- 验证失败反馈增强
  - 自动把运行时错误文案合并进 `last_verification_feedback`
  - 方便看到类似：
    - `error:6wmuR1`
    - `error:yaYU41`
    - `error:zfACW1`

#### 关键默认行为变化

- 之前：无头可能自动优先 `patchright`
- 现在：**无头默认固定优先 `playwright`**

---

### 2）`XianyuAutoAsync.py`

#### 已做修改

- 初始化 Cookie 字符串时，先做 BOM 和空白清理
- 创建 `XianyuSliderStealth` 时补齐参数：
  - `headless=not show_browser`
  - `initial_cookies=self.cookies_str`
  - `proxy=self.proxy_config`

#### 影响

- 后台正式链路会把手动 Cookie 和代理配置完整传给滑块模块
- 不再是“调试脚本能跑、正式流程没带全参数”那种半拉子状态

---

### 3）`utils/xianyu_utils.py`

#### 已做修改

- `trans_cookies()` 更稳：
  - 支持直接按 `;` 分割
  - 自动去 BOM
  - 自动去空格

#### 影响

- 用户直接贴整串 Cookie 时，解析兼容性更好

---

### 4）`debug_manual_cookie_slider.py`

#### 已做修改

- 增加参数：
  - `--automation-backend`
  - `--stealth-mode`

#### 影响

- 便于单独 A/B 测试：
  - `patchright/off`
  - `patchright/full`
  - `playwright/off`
  - `playwright/full`

---

### 5）`.gitignore`

#### 已做修改

- 忽略：
  - `.playwright-browsers/`

#### 影响

- 项目内自动下载的浏览器不会污染 git

---

## 实测过程结论

本次重点做了多组 A/B：

- `patchright + headless + off`
- `patchright + headless + full`
- `playwright + headless + off`
- `playwright + headless + full`

### 观察结果

- `patchright + headless`
  - 有时能过，但不稳定
  - 即便关掉自定义 `init_script`，也经常还是本地校验失败

- `playwright + headless + off`
  - 也会失败

- **`playwright + headless + full`**
  - 成功样本最明确
  - 最终选作项目默认方案

---

## 成功验证记录

### A. A/B Sweep 成功样本

成功 run：

- `sweep_playwright_full_2`

成功特征：

- 验证通过
- 页面跳转到：
  - `https://www.taobao.com/`
- 成功拿到：
  - `x5sec`

成功日志关键点：

- `滑块验证成功`
- `当前页面URL: https://www.taobao.com/`
- `获取到的所有cookie: ... 'x5sec' ...`

---

### B. 默认配置成功样本

为了确认“不是必须手工指定参数才行”，又跑了一次**默认配置**。

成功 run：

- `project_default_verify`

运行参数特征：

- `headless=True`
- `automation_backend=auto`
- `stealth_mode=auto`

实际生效日志显示：

- 后端：`playwright`
- stealth：`full`

结果：

- `success=True`
- `cookie_count=23`
- 获取到 `x5sec`
- 跳转到：
  - `https://www.taobao.com/`

这说明：

> **项目当前默认代码已经可以直接跑通无头滑块，不需要额外手工指定后端。**

---

## 现在的默认策略

### 默认无头策略

- 后端：`playwright`
- stealth：`full`
- warmup：默认关闭
- 浏览器：优先使用项目内 `.playwright-browsers/` 自动下载/复用

### 如果要强制切换

可用环境变量：

```powershell
$env:XY_SLIDER_AUTOMATION_BACKEND='playwright'
$env:XY_SLIDER_STEALTH_MODE='full'
```

如果要强制测试 Patchright：

```powershell
$env:XY_SLIDER_AUTOMATION_BACKEND='patchright'
$env:XY_SLIDER_STEALTH_MODE='off'
```

如果要恢复 warmup：

```powershell
$env:XY_SLIDER_HEADLESS_WARMUP='1'
```

---

## 调试入口命令

### 使用默认策略

```powershell
.\.venv\Scripts\python.exe -u debug_manual_cookie_slider.py --cookie "<cookie>" --cookie-id test --headless --max-retries 1
```

### 显式指定成功方案

```powershell
.\.venv\Scripts\python.exe -u debug_manual_cookie_slider.py --cookie "<cookie>" --cookie-id test --headless --max-retries 1 --automation-backend playwright --stealth-mode full
```

---

## 本次修改的核心价值

这次不是“加了一堆玄学随机数”，核心价值是这几个：

1. **把默认无头链路切到真正跑通的实现**
   - 从不稳定的 `patchright` 默认切回 `playwright`

2. **把浏览器下载/驱动问题内置解决**
   - 不依赖本机已安装浏览器

3. **把 Cookie 注入链路补全**
   - 正式流程和调试流程参数统一

4. **把失败原因从黑箱变成可观测**
   - 以后再出问题，不用靠猜

5. **已经实测证明默认项目代码可通过无头滑块**

---

## 2026-05-02 正式回归验证：`/manual-cookie-import` 已跑通

### 本轮正式验证

- 账号ID：`formal_cookie_headless_verify_20260502`
- 会话ID：`gYySZr4SqjtXqcvB5IUBEg`
- 模式：**无头**
- 接口：`POST /manual-cookie-import`
- 结果：`status=success`
- `cookie_count=25`

### 关键证据

- 项目内浏览器：
  - `realtime.log:26636`
  - `realtime.log:26640`
- full stealth 已注入：
  - `realtime.log:26647`
- 第 1 次滑块成功：
  - `realtime.log:26899`
- 成功取回 Cookie：
  - `realtime.log:26942`
- 新实例成功接管并连上：
  - `realtime.log:26957`
  - `realtime.log:26966`
  - `realtime.log:27000` ~ `27004`

### 运行态核验

- `GET /cookies/formal_cookie_headless_verify_20260502/runtime-status`
  - `instance_exists=true`
  - `running=true`
  - `connection_state=connected`
  - `has_current_token=true`

- `GET /cookie/formal_cookie_headless_verify_20260502/details?include_secrets=false`
  - 返回正常，说明 DB 记录和运行实例都在

这轮不是“调试脚本过了”，而是**正式链自己过了**。这俩不是一回事，别瞎混。

---

## 2026-05-02 同轮顺手清理

1. **去掉后台偷偷强制有头**
   - 删掉了滑块失败恢复链路里偷偷传 `force_show_browser=True` 的行为
   - 现在是否有头，只看账号配置和 `XY_SLIDER_FORCE_HEADFUL`

2. **统一 `cna` 口径**
   - `cna` 从“必需字段”调整为“观察字段”
   - 核心会话校验不再被这个字段误伤，但日志仍会打印

3. **补 Cookie 字符串清洗**
   - 统一清理 BOM 和多余空白
   - `trans_cookies(...)` 改成按 `;` 拆分，兼容更脏的手填 Cookie

4. **删掉无引用 legacy stealth 死代码**
   - `utils/xianyu_slider_stealth.py` 里旧版 `_get_legacy_stealth_script_unused(...)` 已移除
   - 省得一大坨废 JS 继续污染维护视线

---

## 后续建议

1. 先用现在这版项目代码跑正式“手动填 Cookie 后续入口”链路
2. 若后续成功率仍波动，再继续做：
   - 成功轨迹样本积累
   - 自动按成功率选择 profile
   - 进一步缩窄 headless_stable 参数范围
3. 暂时不要把默认后端再切回 `patchright`
   - 这玩意儿这题里不稳，别作妖

---

## 本次涉及文件

- `utils/xianyu_slider_stealth.py`
- `XianyuAutoAsync.py`
- `utils/xianyu_utils.py`
- `reply_server.py`
- `debug_manual_cookie_slider.py`
- `.gitignore`
