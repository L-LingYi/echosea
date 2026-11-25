# Changelog

## [1.1.0] - 2026-11-25

### Fixed (基于线上运行诊断 `小呆呆运行诊断-20261125`)
- **数据库 Schema 对齐与老库无损迁移**：在 `db/init.sql` 补齐 `memory_episodes.embedding_json` 字段，新增幂等迁移脚本 `db/migrations_v1_1.sql` 并在 PostgreSQL 启动时自动安全执行，彻底消除新老库字段差异。
- **探针流量与生产指标彻底隔离**：为 `usage_metrics` 新增 `source`（`production` / `probe` / `sandbox` / `reflection`）字段；仪表盘延迟、P95 TTFT 与费用统计自动排除健康探针样本，并在真实调用成功时自动刷新 `last_health_status = 'healthy'`（解决模型探针长期显示 `unknown` 的问题）。
- **TTS 合成与 OneBot 受理状态拆分**：在 `tts_deliveries` 中拆分 `synthesis_status`（音频合成）与 `onebot_status`（OneBot 受理回执），并关联 `request_id` 与 `onebot_echo`。

### Added
- **统一 `request_id` 全链路脱敏追踪**：新增 `pipeline_traces` 表与 `/api/dashboard/traces` 接口，用同一 `request_id` 串联 `inbound -> gatekeeper -> main_brain -> tts -> onebot` 各阶段耗时与状态，运维日志绝不打印聊天正文。
- **TypeSafe AI Jev 原生门卫协议支持**：在模型总控中心新增 `typesafe_systemone` 协议（原生调用 `POST /v1/systemone` 的 `state` + `questions` 结构并解析校准概率），同时将 `gatekeeper` 槽位正式接入主链路（支持 OpenAI 兼容小模型作为备用路由）。
- **门卫与语音意图标注回归评测**：新增 `POST /api/persona/sandbox/benchmark`，支持使用标注群聊样本批量统计误接（FP）、漏接（FN）与语音意图误判率。
- **意识海（Consciousness Sea）记忆可视化图谱**：新增 `GET /api/memory/graph` 与 WebUI 交互式星海力导向图，直观呈现 L5 自我核心、群聊域、L4 熟人卡、L2 边缘旁听、L3 群情景与私聊半隔离边界、L4 群黑话的关联脉络。
- **WebUI 全面美化与手机端适配**：新增移动端顶部栏、抽屉菜单、底部快捷导航栏、响应式 2×2 KPI 卡片、横向滚动表格与触控手势支持。
