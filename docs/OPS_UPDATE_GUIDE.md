# EchoSea 运维与在线更新指南

EchoSea 采用「特权分离」的宿主机更新守护进程（`ops/updater/host_updater.py`）配合受控 Git 更新源实现在线更新与故障自动回滚。

---

## 首次启用在线更新

### 1. 备份与目录准备

1. 备份现网源码、`.env`、PostgreSQL 数据库与当前运行镜像，并保留原有的 Compose 项目名及数据卷（切换容器期间会短暂中断请求）。
2. 将代码部署到 Linux 主机的 Git 工作区（例如 `/opt/echosea`），保留 `.env`。如需接入外部 SnowLuma Docker 网络，可从 `docker-compose.override.yml.example` 复制创建 `docker-compose.override.yml` 并设置 `SNOWLUMA_DOCKER_NETWORK`。

### 2. 配置更新源

- **公开 / 远程 Git 仓库模式**：在宿主机配置中将 `UPDATER_GIT_SOURCE` 设为受信任的远程仓库地址（如 `https://github.com/<owner>/<repo>.git`），并在 Core 的 `.env` 中将 `UPDATE_GITHUB_REPO` 设为对应的 `<owner>/<repo>`，两者保持同一分支（如 `main`）。
- **本地裸仓库模式（无外网或离线审核发布）**：使用宿主机上由 `root` 管理的 Git 裸仓库作为更新源，通过 `git bundle` 导出并快进更新：

```bash
# 在本地打包发布分支
git bundle create companion-release.bundle main

# 传输到主机受保护目录后，由 root 初始化裸仓库
install -d -m 0700 /var/lib/companion-updater
git clone --bare /path/to/companion-release.bundle /var/lib/companion-updater/source.git
```

### 3. 配置共享密钥与环境变量

生成 32 字节随机密钥（`openssl rand -hex 32`），在 Core 的 `.env` 与宿主机 `/etc/companion-updater.env` 中配置相同的 `UPDATE_WEBHOOK_SECRET`。宿主机配置文件应归 `root` 所有且权限为 `0600`。

```dotenv
# Core .env
UPDATE_AGENT_URL=http://host.docker.internal:9099
UPDATE_WEBHOOK_SECRET=<随机密钥>
UPDATE_GIT_BRANCH=main
# 公开仓库填写 owner/repo；本地裸仓库模式留空
UPDATE_GITHUB_REPO=
```

```dotenv
# 宿主机 /etc/companion-updater.env
COMPANION_REPO_DIR=/opt/echosea
UPDATER_GIT_SOURCE=/var/lib/companion-updater/source.git
UPDATER_GIT_BRANCH=main
UPDATER_BIND_HOST=172.17.0.1
UPDATER_BIND_PORT=9099
COMPANION_CORE_URL=http://127.0.0.1:8000
UPDATE_WEBHOOK_SECRET=<同一随机密钥>
```

> 请将 `UPDATER_BIND_HOST` 设为实际的 Docker 主机网桥地址，可通过 `ip -4 -o addr show dev docker0` 核对；勿将其绑定到 `0.0.0.0` 或公网地址。

### 4. 安装宿主机守护进程并重建 Core

```bash
cd /opt/echosea
sudo ./ops/updater/install_updater.sh "$PWD"

APP_GIT_COMMIT=$(git rev-parse HEAD) docker compose config --quiet
APP_GIT_COMMIT=$(git rev-parse HEAD) docker compose build companion-core
APP_GIT_COMMIT=$(git rev-parse HEAD) docker compose up -d --no-deps --no-build --force-recreate --wait companion-core
curl -fsS http://127.0.0.1:8000/healthz
```

---

## WebUI 更新与自动回滚

1. 登录管理后台，打开 **「运维、插件与更新 → 系统在线更新」**，点击 **「检查新版本」**。
2. 检测到更新后点击安装，任务状态与审计记录会持久化保存；容器重启恢复后可在页面查看执行结果。
3. **安全与回滚机制**：
   - 宿主更新器仅从配置指定的 Git 来源执行快进（Fast-Forward），并校验正在运行的 Core 提交版本。
   - 升级前自动备份 PostgreSQL（保存在 `/var/lib/companion-updater`）并保留旧版 Core 镜像。
   - 若新版本未通过 `/healthz` 或提交校验失败，更新器将自动回退至上一提交与旧镜像。
   - `docker-compose.yml` 仅调整 `companion-core` 环境变量、内存限制与日志轮转等安全字段时可直接自动放行；涉及 `Dockerfile`、挂载卷/端口/网络或其他服务、以及 `ops/updater/` 更新器脚本的变更，可在 WebUI 弹窗查看精确 `git diff` 并通过 TOTP 动态码（或管理员密码）签发一次性确认令牌后在宿主机执行重建。
   - 若升级了 `companion-updater.service` 权限，建议首次执行一次 `sudo ./ops/updater/install_updater.sh "$PWD"` 以允许更新器后续自动热替换 `/usr/local/libexec/companion-updater/host_updater.py` 并平滑重启。

---

## 本次升级（自主联网搜索 & 免 API SearXNG）运维操作说明

本次更新仅修改应用代码与可选 `searxng` 服务编排，**无数据库破坏性变更，无需执行手动 SQL 迁移**。

1. **常规在线/手动升级 Core（必做）**：
   - 如已配置宿主机在线更新器，可直接在管理后台 **「运维、插件与更新 → 系统在线更新」** 点击检查并安装；
   - 若手动更新：
     ```bash
     cd /opt/echosea
     git pull --ff-only
     APP_GIT_COMMIT=$(git rev-parse HEAD) docker compose build companion-core
     APP_GIT_COMMIT=$(git rev-parse HEAD) docker compose up -d --no-deps --no-build --force-recreate --wait companion-core
     curl -fsS http://127.0.0.1:8000/healthz
     ```
   - 仅升级 `companion-core` 后即可**免 API Key 开箱使用**内置免费搜索兜底，不会中断原有业务。

2. **可选启用本地 SearXNG 聚合搜索容器（强烈推荐生产环境开启）**：
   - 本仓库已新增 `ops/searxng/settings.yml` 与 `docker-compose.yml` 中的 `searxng` 服务（`profiles: ["search"]`），启动后 `companion-core` 会自动通过内网 `http://searxng:8080` 调用聚合搜索（支持 Bing / 百度 / 搜狗 / DuckDuckGo / Wikipedia，无需任何 API Key）：
     ```bash
     cd /opt/echosea
     docker compose --profile search up -d searxng
     ```
   - 若服务器已配置 `.env`，可按需核对或补充（默认代码已内建同样默认值，不写也能运行）：
     ```dotenv
     SEARXNG_URL=http://searxng:8080
     DEFAULT_FREE_SEARCH_ENABLED=true
     ```
   - 若管理后台「模型总控中心」的 `web_search` 槽位已配置第三方付费 API（如 Tavily / Serper），系统仍优先使用该配置；删除或禁用后自动回落至 SearXNG / 内置免费检索。
