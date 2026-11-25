# Changelog

## [1.2.0] - 2026-11-25

### Added
- **行业规范级在线更新架构（权限隔离 + HMAC-SHA256 签名 + 失败自动回滚）**：
  - `companion-core` 容器零挂载 `docker.sock`，通过 HMAC-SHA256 签名 + 60 秒防重放时间戳与宿主机轻量更新守护进程（`ops/updater/host_updater.py` + `systemd` 单元）通信。
  - 同时兼容**当前本地仓库模式**与**后期 GitHub 公开仓库模式**（仅需在 `.env` 填入 `UPDATE_GITHUB_REPO=用户名/仓库名` 即可自动检测 GitHub Releases/Commits）。
  - 宿主机更新守护进程在升级前记录旧 Commit，重建 `companion-core` 后轮询 `/healthz`，超时或失败时**自动 `git reset --hard` 回滚并重建**，同时回调 Core 记录审计日志并触发告警。
  - WebUI「🧩 运维、插件与更新」页新增「🔄 系统在线更新」面板，支持手机/桌面一键检查更新、触发更新与查看历史审计记录。

## [1.1.1] - 2026-11-25
- 合并「记忆意识海」统一菜单；完成运维反馈的 6 项现网加固（`127.0.0.1` 回环绑定、`docker-compose.override.yml.example`、事务全量 SQL 迁移、语音三态与失败回退文字、引用会话范围校验、前端全量 XSS 转义、`PATCH /api/providers/{id}` 与 `extra_config` 保护）。
