# Changelog

## [1.3.3] - 2026-11-25（全量累计 Git 发布包）

### Added
- **随时间衰减的单人专注关注度（Time-Decaying Attention Window）**：
  - 群友通过 `@小呆呆`、直呼小名或引用机器人进行强唤醒后，系统按 `(群号, QQ号)` 锁定关注度：**0–60 秒（1 分钟）热聊期**门槛降至 `0.35`（后续搭话无需每次 `@` 或引用）；**60–240 秒（1–4 分钟）衰减期**门槛平滑线性回升至 `0.78`；超过 4 分钟自然归零。
  - 若期间该群友 `@他人`、引用他人消息或说出「好了 / 没事了 / 退下」等结束语，关注度立即清零；同群其他人的聊天完全不受干扰。

### Changed
- **分层超时策略**：`direct_pass`（直接呼唤/私聊）绝不丢弃、超理想耗时仅仪表盘告警；门卫设置 18 秒安全降级上限（防 120 秒死等并记告警）；仅对严重过时的主动插嘴 `soft_entry`（耗时 ≥35 秒且已被新消息刷过 ≥5 条）执行静默放弃。

## [1.3.0 – 1.3.2] - 2026-11-25（已全部包含在本全量包内）
- 非阻塞 WS 派发、智能按需引用、门卫最近 5 条上下文归属感知、主脑 `[发言人归属锚点]`、机器人自身回复入库 L1、表情包语境自动打标、慢响应仪表盘告警与 WebUI 全面视觉精修。

## [1.2.0] - 2026-11-25

- 管理后台新增「系统在线更新」，支持检查受控 Git 更新源、安装版本和查看审计记录。
- 独立宿主机服务使用 HMAC-SHA256、60 秒有效期和持久任务去重。业务容器只访问签名接口。
- 更新前备份 PostgreSQL 并保留旧镜像。健康检测核对实际运行提交，失败时恢复上一提交和旧镜像。
- 保留现网 SnowLuma 令牌、网络覆盖配置和豆包 V3 合成。新增 PostgreSQL 更新审计表。

## [1.1.1] - 2026-11-25

- 合并「记忆意识海」菜单，增加 HTML 转义和模型路由 PATCH 编辑，保留未修改的模型额外配置。
- 按合成、提交及受理记录语音状态。现网 PostgreSQL 迁移保留完整脚本执行。


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
