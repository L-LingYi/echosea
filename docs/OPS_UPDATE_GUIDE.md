# 运维 Git 提交与线上升级指南（v1.1.1）

本压缩包为 **Git 提交就绪版**，默认仅绑定 `127.0.0.1:${APP_PORT:-8000}:8000` 并通过 `.gitignore` 保护现网 `.env` 与 `docker-compose.override.yml`，避免覆盖现网 Nginx / SnowLuma 外部网络配置。

> **停机提示**：现网为单 `companion-core` 实例，重建容器期间会有数秒短暂中断（并非零停机），建议避开群聊高峰窗口执行。

## 1. 同步到 Git 仓库（保留现网配置）

```bash
unzip -o companion-system-v1.1.1-git-release.zip -d /tmp/release_v1_1_1
rsync -av \
  --exclude='.env' --exclude='.env.*' \
  --exclude='docker-compose.override.yml' \
  --exclude='backups/' --exclude='data/' --exclude='logs/' \
  /tmp/release_v1_1_1/companion-system/ ./

git status
git add -A
git commit -m "fix(v1.1.1): unify Memory Sea UI, harden XSS/quote scope, 3-state TTS fallback, safe SQL migrations & PATCH providers"
git push origin main
```

## 2. 现网 SnowLuma 外部网络接入（一次性配置，不再被发版覆盖）

如现网通过外部 Docker 网络与 SnowLuma 互通，复制 `docker-compose.override.yml.example` 为 `docker-compose.override.yml`（已被 `.gitignore` 忽略），填入实际网络名；如 HTTP API 使用独立令牌，在 `.env` 中配置 `SNOWLUMA_HTTP_TOKEN`。

## 3. 数据库幂等升级与重建容器

Core 启动时会在 PostgreSQL 事务中安全执行整份 `db/migrations_v1_1.sql`（已修复注释行跳过问题），也可提前手动校验：

```bash
docker exec -i companion-postgres psql -U companion -d companion < db/migrations_v1_1.sql
docker compose up -d --build companion-core
```

## 4. 切换后可靠验收命令（不依赖 INFO 日志）

```bash
# 1. Core 健康与版本
curl -s http://127.0.0.1:8000/healthz

# 2. 登录后检查 SnowLuma 网关在线状态与仪表盘口径（需替换管理员密码）
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<YOUR_ADMIN_PASSWORD>"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

curl -s http://127.0.0.1:8000/api/onebot/status -H "Authorization: Bearer $TOKEN"
curl -s http://127.0.0.1:8000/api/dashboard/overview?days=7 -H "Authorization: Bearer $TOKEN"
```


## 5. 启用「系统在线更新」守护进程（行业规范权限隔离架构）

`companion-core` 容器不挂载 `docker.sock`，而是通过宿主机上的轻量 `systemd` 服务（`ops/updater/host_updater.py`）以 **HMAC-SHA256 签名 + 60 秒防重放** 执行拉取、重建和失败自动回滚：

```bash
# 1. 在 .env 中生成并写入共享签名密钥：
#    UPDATE_WEBHOOK_SECRET=$(openssl rand -hex 32)
#    UPDATE_AGENT_URL=http://host.docker.internal:9099
#    UPDATE_GITHUB_REPO=           # 当前本地仓库留空；后期公开仓库填 owner/repo

# 2. 在宿主机一键安装并启动守护进程（仅监听 Docker 网桥 / 本机，不暴露公网）
chmod +x ops/updater/install_updater.sh
sudo ./ops/updater/install_updater.sh /opt/companion-system

# 3. 重启 Core 使新环境变量生效
docker compose up -d --build companion-core
```

此后即可直接在手机或电脑 WebUI 的「🧩 运维、插件与更新 → 🔄 系统在线更新」中点击「检查新版本」与「一键安全更新」。若新版本健康检查失败，宿主机守护进程会自动 `git reset --hard` 回滚到上一提交并推送告警。
