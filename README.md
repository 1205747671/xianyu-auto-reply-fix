# 闲鱼管理系统正式版

## 说明

当前仓库为正式版运行代码，已移除在线更新、外部公告及附加展示内容。

## 启动

- 本地启动：`python Start.py`
- 默认地址：`http://localhost:8090`

## 部署

- 容器部署：`docker compose up -d --build`
- 国内构建：`docker compose -f docker-compose-cn.yml up -d --build`

## 升级

请通过代码发布、镜像构建或容器部署流程完成升级，不使用后台在线升级。
