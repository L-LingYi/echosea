# 运维 Git 提交与线上零停机升级指南（v1.0.0 → v1.1.0）

本压缩包为 **Git 提交就绪版（Commit-Ready Release）**，已排除 `.env`、缓存与本地数据文件，可直接覆盖至 Git 仓库根目录提交并滚动更新线上实例。

## 1. 提交到 Git 仓库

```bash
# 在你的本地/服务器 Git 仓库根目录执行（注意保留服务器上的 .env，勿覆盖）
unzip -o companion-system-v1.1.0-git-release.zip -d /tmp/release_v1_1
rsync -av --exclude='.env' --exclude='backups/' --exclude='data/' /tmp/release_v1_1/companion-system/ ./

git status
git add -A
git commit -m "feat(v1.1.0): diagnostics observability, Jev System One gatekeeper, Consciousness Sea UI & mobile layout"
git push origin main
```

## 2. 线上 PostgreSQL 无损升级（幂等安全）

Core 启动时会自动执行 `db/migrations_v1_1.sql`，也可提前手动执行确认（重复执行完全幂等、不丢数据）：

```bash
docker exec -i companion-postgres psql -U companion -d companion < db/migrations_v1_1.sql
```

## 3. 重建并平滑重启 Core 容器

```bash
docker compose up -d --build companion-core
docker compose ps
curl -s http://127.0.0.1:8000/healthz
```

## 4. 升级后快速验收清单

1. 用手机浏览器打开管理后台，确认顶部抽屉菜单、底部快捷导航和「🌌 意识海 · 记忆图谱」均可触控缩放与点击查看详情。
2. 在「🧠 模型总控中心」新增 `gatekeeper` 槽位：协议选 `typesafe_systemone`，Base URL 填 `https://api.typesafe.ai`，Model 填 `jev-1.14`（或你账号开通的 Jev 模型名），点击「测活」验证。
3. 在「🔎 全链路追踪」检查新消息是否已通过统一 `request_id` 记录 `inbound → gatekeeper → main_brain → tts → onebot` 各阶段耗时，且不含任何聊天正文。
