# 正式流程对齐调整说明（2026-05-01）

## 结论

本次把项目里两条正式流程对齐到了已验证的单次调试链路：

1. **手动填入 Cookie**
   - 不再只是把 Cookie 直接塞进数据库就完事。
   - 现在会先走**单次真实浏览器滑块验证链路**，验证成功后再保存到正式账号。

2. **账号密码登录**
   - 保持之前已修好的正式链路。
   - 继续走真实浏览器、无头优先、自动过滑块、后续把二维码/人脸验证信息抛给前端。

一句话说透：

> 现在正式前端不再和 debug 脚本两张皮，手动 Cookie 和账密登录都已经接到同一套可复现的真实浏览器链路上。

---

## 本次改动文件

- `utils/xianyu_slider_stealth.py`
- `debug_manual_cookie_slider.py`
- `reply_server.py`
- `static/js/app.js`
- `static/index.html`

---

## 具体调整

### 1. 手动 Cookie：新增正式“导入并验证”链路

新增正式接口：

- `POST /manual-cookie-import`
- `GET /manual-cookie-import/check/{session_id}`

行为：

- 前端提交账号ID + Cookie
- 后端先用 Cookie 预检最新 `verification_url`
- 再调用 `XianyuSliderStealth.run(...)`
- 成功后把浏览器拿回来的 Cookie 与原始 Cookie 做保护性合并
- 最后才写入数据库并更新 `cookie_manager`

这就避免了以前那种：

- 只是先保存脏 Cookie
- 后台自己再进入无限重试
- 前端看着像“加进去了”，实际上滑块根本没收口

### 2. 把 Cookie 预检逻辑抽到正式代码里

从 debug 脚本里提炼并复用：

- `parse_cookie_string(...)`
- `generate_cookie_verification_device_id(...)`
- `build_cookie_verification_sign(...)`
- `resolve_verification_url_from_cookie(...)`

这样正式流程和 debug 流程就不是各写各的野路子了。

### 3. 前端“导入 Cookie”按钮改成异步会话流程

原来：

- `导入 Cookie` -> 直接 `POST /cookies`

现在：

- `导入并验证账号` -> `POST /manual-cookie-import`
- 前端轮询 `/manual-cookie-import/check/{session_id}`
- 成功/失败由正式状态返回

并补了：

- 按钮 loading 状态
- 会话轮询
- 成功/失败提示
- 与现有验证弹窗的兼容

### 4. 账号密码登录保持正式链路可用

本次没有推翻之前修好的账密链路，只是继续验证正式流程没被这次改动带崩。

当前正式账密流程仍然是：

- 无头真实浏览器打开登录页
- 自动输入账号密码
- 自动过滑块
- 如果命中二维码/人脸验证，前端弹窗展示截图并继续轮询

---

## 实测结果

## A. 手动 Cookie —— 正式接口测试：成功

使用用户提供测试 Cookie：

- 触发正式接口：`POST /manual-cookie-import`
- 再轮询：`GET /manual-cookie-import/check/{session_id}`

最终结果：

```json
{
  "status": "success",
  "message": "账号 formal_cookie_flow_after_patch_20260501 Cookie 导入并验证成功",
  "account_id": "formal_cookie_flow_after_patch_20260501",
  "is_new_account": true,
  "cookie_count": 23
}
```

说明：

- 这条链路已经不是“只存 Cookie”
- 而是**正式流程里真实跑浏览器过完当前滑块后再入库**

---

## B. 手动 Cookie —— 正式前端测试：成功

前端实际操作：

- 进入 `/admin`
- 打开“账号管理”
- 点击“导入并验证账号”
- 填入测试 Cookie
- 等待正式前端轮询收口

前端结果：

```text
账号 formal_ui_cookie_after_patch_20260501 导入并验证成功
```

证据：

- 前端截图：`logs/formal_ui_cookie_after_patch.png`

数据库落库检查：

- `formal_cookie_flow_after_patch_20260501`
- `formal_ui_cookie_after_patch_20260501`

两条记录都已成功保存。

---

## C. 账号密码登录 —— 正式接口测试：通过到二维码验证

测试账号（已脱敏）：

- 用户名：`131****6772`
- 密码：`<已脱敏>`

正式接口结果：

```json
{
  "status": "verification_required",
  "verification_url": null,
  "screenshot_path": "static/uploads/images/face_verify_formal_password_flow_after_patch_20260501_20260501_172836.jpg",
  "qr_code_url": null,
  "verification_type": "二维码验证",
  "message": "需要二维码验证，请查看验证截图"
}
```

说明：

- 自动进表单：正常
- 自动过滑块：正常
- 后续验证抛出：正常

当前阻塞点已经不是滑块，而是这个账号自身命中了**二维码验证**。

---

## D. 账号密码登录 —— 正式前端测试：通过到二维码验证

前端实际操作：

- 进入 `/admin`
- 打开“账号管理”
- 点击“账密登录”
- 提交账号密码

前端结果：

- 成功弹出验证模态框
- 成功加载二维码验证截图

关键结果：

```text
需要闲鱼二维码验证，请使用手机闲鱼APP扫描下方二维码完成验证
```

截图地址：

```text
/static/uploads/images/face_verify_formal_ui_password_after_patch_20260501_20260501_173126.jpg
```

证据：

- 前端截图：`logs/formal_ui_password_after_patch.png`

---

## 当前正式流程状态

### 手动 Cookie

已完成正式接入，并已**真实测试成功**。

### 账号密码登录

已完成正式接入，并已确认：

- 无头滑块链路正常
- 正式前端/接口都能接住后续二维码验证

当前是否“最终登录完成”取决于该账号后续是否需要人工扫码/人脸。

---

## 这次改完后，正式流程的实际变化

### 原来

- 导入 Cookie：只是保存
- 是否能过滑块：靠后台后续自己撞
- debug 能跑，不代表正式入口能跑

### 现在

- 导入 Cookie：正式前端直接走单次真实浏览器验证
- 账密登录：正式前端直接走单次真实浏览器登录 + 滑块 + 后续验证展示
- debug 跟正式入口已经接到同一条链路上

---

## 建议下一步

如果要把账密这题彻底收口，下一步不是继续折腾滑块，而是：

1. 用正式前端打开二维码验证弹窗
2. 手机闲鱼扫码/做人脸
3. 验证正式轮询是否会从 `verification_required` 自动切到 `success`

这才是后半段闭环。
