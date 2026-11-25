# Companion System v1.2.0 运维更新指南

## 首次启用在线更新

1. 备份现网源码、`.env`、PostgreSQL 和运行镜像。保留现网 Compose 项目名及数据卷。现网是单 Core 实例，切换容器会短暂中断请求。
2. 将审核过的提交部署到 Linux 主机的 Git 工作区。香港云的部署目录是 `/opt/companion-system-v1`。保留 `.env`，从 `docker-compose.override.yml.example` 创建本机覆盖文件。香港云应设置 `SNOWLUMA_DOCKER_NETWORK=astrbot-napcat_astrbot_network`，原有 `SNOWLUMA_HTTP_ACCESS_TOKEN` 继续生效。
3. 本地仓库模式使用宿主机上由 root 管理的 Git 裸仓库作为更新源。例如将本地 `main` 导出为 Git bundle，传到主机并用 `git clone --bare` 初始化 `/var/lib/companion-updater/source.git`。后续在本地生成新 bundle，由运维把其中的 `main` 快进到裸仓库。确认快进后再打开后台检查更新。

```bash
# 本地仓库运行
git bundle create companion-release.bundle main

# 已将 bundle 传到主机的受保护目录后，由 root 运行
install -d -m 0700 /var/lib/companion-updater
git clone --bare /path/to/companion-release.bundle /var/lib/companion-updater/source.git
```

4. 生成一次 32 字节随机密钥（`openssl rand -hex 32`）。在 Core 的 `.env` 和宿主机 `/etc/companion-updater.env` 中配置相同的 `UPDATE_WEBHOOK_SECRET`。两个文件都限制访问权限，宿主机文件必须归 root 所有且为 `0600`。

```dotenv
# Core .env
UPDATE_AGENT_URL=http://host.docker.internal:9099
UPDATE_WEBHOOK_SECRET=<随机密钥>
UPDATE_GIT_BRANCH=main
UPDATE_GITHUB_REPO=
```

```dotenv
# 宿主机 /etc/companion-updater.env
COMPANION_REPO_DIR=/opt/companion-system-v1
UPDATER_GIT_SOURCE=/var/lib/companion-updater/source.git
UPDATER_GIT_BRANCH=main
UPDATER_BIND_HOST=172.17.0.1
UPDATER_BIND_PORT=9099
COMPANION_CORE_URL=http://127.0.0.1:8000
UPDATE_WEBHOOK_SECRET=<同一随机密钥>
```

将 `UPDATER_BIND_HOST` 设为实际的 Docker 主机网桥地址，可用 `ip -4 -o addr show dev docker0` 核对。宿主服务安装到独立系统目录。业务容器只访问签名接口。

```bash
cd /opt/companion-system-v1
sudo ./ops/updater/install_updater.sh "$PWD"
APP_GIT_COMMIT=$(git rev-parse HEAD) docker compose config --quiet
APP_GIT_COMMIT=$(git rev-parse HEAD) docker compose build companion-core
APP_GIT_COMMIT=$(git rev-parse HEAD) docker compose up -d --no-deps --no-build --force-recreate --wait companion-core
curl -fsS http://127.0.0.1:8000/healthz
```

## WebUI 更新和回滚

登录后台后打开「运维、插件与更新 → 系统在线更新」，点击「检查新版本」。更新源有更新时可安装。任务结果保存在后台历史中，重启期间可在服务恢复后重新打开页面查询。

宿主更新器只从其配置指定的 Git 来源快进，并校验正在运行的 Core 提交。Compose、Dockerfile 等部署定义变更由运维在宿主机审核并部署。更新器自身的变更审核后重新运行安装脚本生效。

实际升级前会备份 PostgreSQL，保留之前的 Core 镜像。新提交未通过 `/healthz` 时恢复上一提交和镜像。迁移应保持向后兼容。数据库备份保存在 `/var/lib/companion-updater`，恢复备份应先确认需要覆盖备份之后的写入。

后续迁移到公开 GitHub 仓库时，在宿主机配置中把 `UPDATER_GIT_SOURCE` 设为审核同意的 `https://github.com/owner/repo.git`，并重启守护进程。Core 的 `UPDATE_GITHUB_REPO` 设为同一个 `owner/repo`，与宿主机使用相同分支。宿主机只接受该分支上的更新提交或该分支历史上的对应发布标签。
