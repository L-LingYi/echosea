# EchoSea（回声海）

> 版本：`v1.9.0` ｜ 面向 Linux / Docker 云端 24 小时运行的独立拟人陪伴系统，原生适配 **SnowLuma（OneBot v11）** 网关。

EchoSea 围绕「群聊优先、兼顾私聊」的真实社交节奏设计，内置三层归属门卫、五层半隔离记忆、多模态看图与表情包图库、豆包 TTS 语音合成、作息状态机与全链路运维控制台。

---

## 核心特性

### 1. 网关接入与防风控
- **SnowLuma OneBot v11 适配**：支持反向 WebSocket（`/ws/onebot/v11`）、正向 WebSocket 客户端自愈与 HTTP 回调。
- **身份与风控护栏**：以 QQ 号为主键同步群名片与历史曾用名（`known_names`），支持引用自动展开、撤回即时拦截、Outbox 审计，以及单群 / 全局发送限流。

### 2. 三层归属门卫与注意力机制
- **精准归属识别**：识别 `@自己`、`@他人`、引用回复与直呼小名/别名，区分直接呼唤与第三人称议论；结合 Redis 活跃互聊状态防插嘴，取消随机概率。
- **动态专注度与护栏**：强唤醒后进入短时热聊窗口（低门槛连续接话）并随时间线性衰减；支持降权软切入与连续轮数上限护栏。

### 3. 五层深度记忆与凌晨反思
- **PostgreSQL 17 + pgvector**：
  - **L1 工作记忆**：近期对话流与 30 分钟群转私聊上下文承接。
  - **L2 边缘旁听**：群聊背景摘要。
  - **L3 情景与纪念日**：关键事件与互动纪念日记录。
  - **L4 熟人卡与群黑话**：SQL 级群聊 / 私聊严格半隔离（私聊隐私绝不带入群聊），支持群黑话待审入库与一键忘却连根清理。
  - **L5 自我状态**：驱动角色自身精力、心情与当前活动。
- **凌晨离线反思引擎**：定时从 L1/L2 对话流中提炼群友公开画像、新黑话与纪念日。

### 4. 多模态看图、表情包与语音管线
- **原生看图与去 AI 味**：dHash 感知哈希旧图缓存 + 3×3 GIF 抽帧拼图；Prompt Cache 静态前缀布局；禁词过滤、去 Markdown 噪点与自然拆句发送。
- **表情包自学习与图库**：感知哈希去重、群内高频与正向反应自动偷图、本地 `.json`/`.jsonl` 预设导入与手工批量上传、WebUI 审核以及 `[sticker:tag]` 自动斗图。
- **豆包 TTS 语音管线**：支持「用语音回答 / 引用后说用语音 / 别发语音」指令识别，口播前清洗 Markdown 与括号动作，支持情绪语速映射、时长保护与超时自动降级文字。

### 5. 联网感知、作息日程与插件生态
- **大模型自主联网感知（免 API 开箱即用）**：慢通道抓取 RSS / JSON 信源提炼「今日刷网简报」融入常识；快通道由主脑大模型通过标准 Function Calling（`web_search` / `fetch_webpage`）自主判断是否需要搜索并提炼关键词，先发轻量过渡语不卡聊，再在独立 `search:<request_id>` 会话中完成检索与自然转述；搜索执行层支持 **后台 `web_search` 槽位 → 本地 SearXNG 容器（`--profile search`）→ 内置免 API 免费检索兜底** 三级路由。
- **24 小时作息与插件 SDK**：作息日程驱动 L5 状态；提供 `CompanionPlugin` 标准 SDK 与异步事件总线（独立超时与异常隔离），内置自动偷图与异地备份同步告警插件。

### 6. 云端运维控制台（WebUI）
- **模型总控中心**：6 大能力槽位主备无缝切换、HTTP 429/402 自动熔断冷却与半开自愈、API Key 使用 AES-256-GCM 加密存库、一键测活。
- **可观测性与安全**：管理员登录 + TOTP 二步验证、全链路 `request_id` 追踪、Token 与语音字符消耗统计图表、深色模式、多渠道告警与群聊回放沙盒。
- **数据备份与在线更新**：支持本地快照、阿里云盘（OpenAPI / WebDAV）定时自动备份，以及特权分离的宿主机在线更新机制。

---

## 快速部署（Linux + Docker）

### 1. 准备环境变量

```bash
cp .env.example .env
```

编辑 `.env`，**务必替换**以下默认配置后再启动：

| 变量 | 说明 |
| :--- | :--- |
| `POSTGRES_PASSWORD` / `DATABASE_URL` | PostgreSQL 数据库密码与连接串（两者密码需保持一致） |
| `MASTER_ENCRYPTION_KEY` | 32 字节加密主密钥（可用 `openssl rand -base64 32` 生成） |
| `JWT_SECRET_KEY` | JWT 签名密钥（可用 `openssl rand -base64 32` 生成） |
| `ADMIN_DEFAULT_USERNAME` / `ADMIN_DEFAULT_PASSWORD` | 首次启动初始化的后台管理员账密 |
| `SNOWLUMA_ACCESS_TOKEN` | SnowLuma OneBot v11 鉴权 Token |
| `BOT_QQ` / `ADMIN_QQ` | 机器人 QQ 号与管理员 QQ 号 |

### 2. 启动服务

```bash
# 基础启动（Core + PostgreSQL + Redis）
docker compose up -d --build

# 完整启动（含 Caddy 自动 HTTPS、每日数据库备份与免 API SearXNG 聚合搜索）
docker compose --profile https --profile backup --profile search up -d --build
```

> 默认仅监听 `127.0.0.1:8000`；如需绑定域名并启用自动 HTTPS，请配置 `Caddyfile` 与 `COMPANION_DOMAIN` 后使用 `--profile https` 启动。

### 3. 登录管理后台

- 访问 `http://<服务器IP>:8000`（或已配置的 HTTPS 域名）。
- **初始账号**：仅在首次启动且数据库 `admin_users` 表为空时，系统会按 `.env` 中的 `ADMIN_DEFAULT_USERNAME` / `ADMIN_DEFAULT_PASSWORD` 自动创建管理员（`.env.example` 默认为 `admin` / `ChangeMeStrongPass2026!`，代码兜底默认密码为 `companion2026`）。
- **安全建议**：系统**不会在登录时强制要求改密**。强烈建议首次登录后立即前往 **「运维、插件与更新 → 管理员安全设置」** 修改密码并绑定 **TOTP 二步验证**。

---

## SnowLuma 网关对接

- **反向 WebSocket（推荐）**：在 SnowLuma 侧配置反向 WS 地址：
  ```text
  ws://<服务器IP>:8000/ws/onebot/v11?access_token=<SNOWLUMA_ACCESS_TOKEN>
  ```
- **正向 WebSocket**：在 `.env` 中设置 `SNOWLUMA_MODE=forward_ws` 与 `SNOWLUMA_WS_URL=ws://snowluma:3001/`。
- 若与外部 SnowLuma 容器同机部署，可参考 `docker-compose.override.yml.example` 复制为 `docker-compose.override.yml` 以挂载外部 Docker 网络。

---

## 相关文档

- [在线更新与运维指南](docs/OPS_UPDATE_GUIDE.md) — 特权分离宿主机更新器配置、升级与回滚流程
- [更新日志](CHANGELOG.md) — 各版本功能演进与修复记录

---

## 本地开发与自动化测试

```bash
pip install -r requirements.txt
PYTHONPATH=backend python3 -m pytest -v
```
