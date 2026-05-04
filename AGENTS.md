# Repository Guidelines

## 项目结构与模块组织
- `Start.py` 是本地启动入口，负责拉起 FastAPI 与后台任务。
- `reply_server.py` 提供 API、页面与流式接口。
- 根目录业务模块包括 `cookie_manager.py`、`db_manager.py`、`ai_reply_engine.py`、`order_*`。
- `utils/` 存放通用工具；`static/` 存放前端资源（`js/`、`css/`、`userscripts/`、`lib/`）；`Dockerfile*`、`docker-compose*.yml`、`nginx/` 用于部署。
- 运行期数据应放在 `data/`、`logs/`、`browser_data/`、`update_backup/`，不要提交到版本库。

## 构建、测试与开发命令
- `python -m venv venv` / `venv\Scripts\activate`：创建并启用虚拟环境。
- `pip install -r requirements.txt`：安装 Python 依赖。
- `playwright install chromium`：安装浏览器自动化依赖。
- `python Start.py`：本地启动，默认地址 `http://localhost:8090`。
- `docker compose up -d --build`：按默认配置启动容器。
- `docker compose -f docker-compose-cn.yml up -d --build`：使用国内构建配置。
- `python release_precheck.py`：发布前检查热更新清单与版本文件。

## 编码风格与命名规范
- Python 统一使用 4 空格缩进、`snake_case` 命名和 `UPPER_CASE` 常量。
- 导入顺序保持“标准库 / 第三方 / 本地模块”。
- 优先沿用现有模块边界，不要把新逻辑直接堆进 `reply_server.py`。
- 前端改动优先复用 `static/js/app.js` 现有函数；样式按功能拆分到 `static/css/*.css`。
- `static/lib/` 为第三方静态资源，仅在升级依赖时修改。

## 测试指南
- 当前仓库没有成型的自动化测试套件。
- 提交前至少运行 `python release_precheck.py`，并手工验证 `/health`、`/docs`、登录、账号管理、订单与自动回复相关流程。
- 如新增自动化测试，请放在 `tests/`，文件命名采用 `test_<feature>.py`，并在 PR 中写明运行方式。

## 提交与 Pull Request 规范
- 提交信息遵循现有风格：`fix ...`、`add ...`、`docs: ...`，例如 `fix password verification refresh timeout`。
- 每次提交只处理一个主题，避免把无关改动揉在一起。
- PR 需说明变更范围、配置或数据迁移影响、手工验证步骤和关联 issue。
- 涉及 `static/` 页面或交互改动时，附截图或 GIF。

## 安全与配置提示
- 敏感信息仅保存在环境变量或本地配置中。
- 不要提交真实 cookie、token、数据库文件或二维码截图。
- 涉及热更新资源时，同步更新 `static/version.txt`，并重新运行 `python release_precheck.py`。
