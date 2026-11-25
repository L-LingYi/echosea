# Companion System v1.1.0 运维更新指南

在本地仓库中保留升级前的基线标签，并提交发行包和当前部署所需的兼容修正。用 `git archive` 从已验证的提交生成部署包。仓库配置了远程时，可按现有流程推送。

## 升级现有 Docker Compose 部署

1. 在现有部署目录记录 Core 的镜像 ID，并给该镜像打回滚标签。备份源码文件，用 `pg_dump --format=custom` 备份 PostgreSQL，再用 `pg_restore --list` 检查备份。
2. 解包并核对变更。保留服务器的 `.env`、现有 Compose 项目名和数据库、Redis、表情包数据卷。香港云部署使用本机回环端口、外部网络 `astrbot-napcat_astrbot_network`、SnowLuma 正向 WebSocket 及单独的 HTTP 出站 Token。
3. 构建新镜像后，仅重建 Core。单实例切换时会短暂中断请求，SnowLuma 正向 WebSocket 随应用启动重新连接。

```bash
docker compose config --quiet
docker compose build companion-core
docker compose up -d --no-deps --no-build --force-recreate --wait companion-core
docker compose ps
curl --fail --silent --show-error http://127.0.0.1:8000/healthz
```

Core 启动时会在 PostgreSQL 事务中执行 `db/migrations_v1_1.sql`，脚本支持重复执行。如需提前迁移，可执行以下命令。

```bash
docker compose exec -T postgres sh -ec 'psql -X -v ON_ERROR_STOP=1 --single-transaction -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < db/migrations_v1_1.sql
```

## 验收与回滚

1. 核对健康接口的版本为 `1.1.0`，确认 PostgreSQL、Redis 正常运行，SnowLuma 已连接。
2. 登录后台，检查「全链路追踪」和「意识海」。追踪记录包含阶段、状态和耗时。OneBot HTTP 受理以成功回执为准，WebSocket 已提交的消息单独标识。
3. 使用已配置的账号凭据时，可在「模型总控中心」测活 `typesafe_systemone` 门卫模型。用真实 QQ 消息验收文字及语音时，同时确认 QQ 客户端收到回复并能播放语音。

若 Core 启动失败，恢复部署源码和原 Compose 配置，将回滚镜像重新标记为 Compose 使用的 Core 镜像，再以 `--no-deps --no-build --force-recreate` 重建 Core。新增数据库列和追踪表向后兼容。数据库恢复会覆盖备份之后的写入，只有确认需要恢复数据时才使用备份。
