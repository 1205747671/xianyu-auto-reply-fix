# 闲鱼账号验证 / Cookie / 保活链路修复总结（更新至 2026-05-06）

## 当前结论

截至 **2026-05-06**，这条链路现在要分两层看：

1. **浏览器运行时已经统一**
   - 仓库里的活跃浏览器链路已经统一走 `CloakBrowser` provider。
   - 启动、自检、Docker 构建、README 安装提示都统一为：
     - `python -m cloakbrowser install`
   - 旧浏览器直连和历史废弃入口已经从活跃路径里收口，`utils/slider_patch.py`、`utils/refresh_util.py` 已删除。

2. **账号密码登录前半段不是当前主要矛盾**
   - 登录页打开、账号密码提交、滑块接管、验证码截图更新，这一段逻辑已经打通。
   - 真正需要盯的，仍然是 **滑块之后的账号风控承接**，包括：
     - 二维码验证
     - 人脸验证
     - `token_refresh` 命中的风控恢复

3. **`token_refresh` 失败不等于“浏览器又坏了”**
   - 现在更常见的情况是：账号进入风险验证页，或者保活链路命中了处罚页/验证页。
   - 重点应该看风控日志、验证截图刷新情况、会话状态，而不是一上来就甩锅给滑块。

一句话说透：

> 现在主要问题不是“老浏览器被识别导致链路根本跑不起来”，而是“账号在滑块后还要不要继续做人工验证，以及保活恢复能不能正确承接这个状态”。

---

## 这轮迁移收口了什么

### 1. 运行时统一到 CloakBrowser

- `requirements.txt`：浏览器依赖改成 `cloakbrowser`
- `Start.py`：启动自检改成检查 `CloakBrowser` provider 和 runtime
- `Dockerfile` / `Dockerfile-cn`：构建阶段改为执行 `python -m cloakbrowser install`
- `docker-compose.yml` / `docker-compose-cn.yml`：保留代理构建参数透传
- `README.md`：安装和访问口径同步到当前配置

### 2. 活跃浏览器链路已统一

以下活跃路径已接到统一 provider：

- 主登录 / 滑块链路
- `token_refresh` 浏览器恢复链路
- 搜索旁路
- 订单详情抓取
- 二维码相关链路
- 远程验证码控制链路

### 3. 删除旧尸体

已删除：

- `utils/slider_patch.py`
- `utils/refresh_util.py`

这些文件继续留着只有一个作用：把人看晕，顺便误导后续排查方向。

---

## 当前排查重点

### A. 看 `token_refresh` 风控恢复有没有接住

重点关注：

- 是否命中 `token_refresh` 场景的风控日志
- 验证截图是否持续刷新
- 页面是否已经进入二维码 / 人脸验证
- 恢复链路是否错误地把处罚页当成普通滑块页

### B. 看账号是不是已经进入人工验证状态

如果日志和截图已经明确进入：

- 二维码验证
- 人脸验证

那这不是“账号密码登录没通”，而是 **账号被要求继续做人机验证**。

### C. 不要乱删账号级浏览器状态

下面这个目录属于运行期关键状态：

- `browser_data/user_<account_id>`

这个目录里保存了账号级 profile、站点状态和恢复链路需要的上下文。  
别手一抖清了，清完再说“怎么又得重新验证”，那就是自己给自己找活。

---

## 本地验证建议

### 1. 先确认 runtime

```powershell
.\.venv\Scripts\python.exe -m cloakbrowser info
.\.venv\Scripts\python.exe -m cloakbrowser install
```

### 2. 本地直接启动

```powershell
.\.venv\Scripts\python.exe Start.py
```

默认地址：

- `http://localhost:8090`
- `http://localhost:8090/docs`
- `http://localhost:8090/health`

### 3. 本地调试账号密码链路

优先先做本地非 Docker 验证，再决定是否同步到 Linux。

建议检查：

- 登录页是否正常打开
- 提交账号密码后是否正确进入滑块
- 滑块后是否进入二维码 / 人脸验证
- 验证截图是否持续刷新
- 风控日志是否和页面状态一致

---

## Linux / Docker 侧注意事项

如果要在 Linux 上重建：

```bash
export HTTPS_PROXY=http://192.168.31.188:10809
docker compose -f docker-compose-cn.yml up -d --build
```

如果下载依赖走本地代理，也可以使用：

- `http://127.0.0.1:1081`

当前仓库已经把构建参数透传到 Docker build，不需要再手工改 Dockerfile 逻辑。

---

## 现在的判断标准

### 不是问题本身的现象

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

1. **浏览器运行时统一为 CloakBrowser**
2. **不再保留旧浏览器 fallback 作为正式方案**
3. **本地先验证，过了再同步 Linux**
4. **后续重点排查账号风控承接，不再重复纠缠旧浏览器栈**
