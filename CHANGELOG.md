# 更新日志（Changelog）

## v1.9.4 (2026-11-27)
- **表情包发图频率节流、防重复与偷图自学习修复**：
  - **发图冷却与轮次间隔**：新增 `STICKER_COOLDOWN_SECONDS=180` 与 `STICKER_MIN_REPLY_GAP=3`（经 `StateStore` 持久化记录每会话上次发图时间与无图回复计数），同时收敛 `prompt_builder.py` 斗图配图提示词为低频克制触发；`StickerService.pick_sticker_for_tag()` 规避最近已发的 12 张表情，并在审核/导入/删图后即时失效 `top_tags` 缓存。
  - **自动偷图权限容错与照片过滤**：`Dockerfile` 预创建 `/app/data/stickers` 并移交 `companion` 用户权限；`StickerService` 本地缓存落盘增加 `OSError` 安全降级与再次出现时自动回填，避免权限或磁盘异常中断偷图入库；`AutoStickerReactionPlugin` 过滤显式普通照片/截图（`sub_type=0`），专注采集 `mface`、表情包（`sub_type=1` / 摘要含「表情」/ GIF）；OneBot v11 发送正式采用标准表情段（`sub_type=1`）并支持本地 `base64://` 回退。
  - **视觉辅助增强打标与反思清理**：`auto_tag_with_context` 优先结合 `ImageVisionCache` 画面摘要与聊天上下文生成精准中文情绪标签；夜间反思 `_review_pending_stickers()` 在入审前尝试回填或淘汰已失效的 QQ 临时链接（`multimedia.nt.qq.com.cn` / `qpic.cn`）。
- **思考模型辅助调用静默失效修复与多模态格式增强**：
  - 修复 `deepseek` / `qwen` 等思考模型在 `vision_caption`（图片后台摘要）、`reflection`（夜间反思与表情自动打标）及节点探活中未关闭思考导致耗尽 `max_tokens`、正文返回为空的问题：`ModelHubService` 在 `disable_thinking=True` 时同步下发 `thinking={"type": "disabled"}` 与 `reasoning_effort="none"`，并在出现空正文配非空 `reasoning_content` 时记录告警；提高 `vision_caption`（120）、`auto_tag`（240）与反思提取（1500）输出上限。
  - `MultimodalService.lookup_or_prepare()` 改为按文件头魔数自动识别 PNG / GIF / JPEG / WebP MIME 类型及动态 GIF（不再仅依赖 `.gif` URL 后缀）。
- **人物归属认知与意识海体验优化**：
  - 修复群聊/私聊中提及、引用或直呼别名（含群名片与 `preferred_address`）关联其他群友时注入其公开 4D 画像；隔离 3D 意识海层叠上下文，确保弹窗抽屉不被星图遮挡。

## v1.9.3 (2026-11-27)
- **3D 记忆意识海重构与 WebUI 深海观测站视觉升级**：
  - **3D 发光星海与悬停名片/记忆摘要**：将 `frontend/js/sea.js` 升级为零依赖 3D 轨道相机星图，默认以发光顶点 + 精简名称呈现，中心始终锁定在 `EchoSea · 意识核心` 并自动避开右侧详情抽屉占位；鼠标悬浮光点即弹出对应人物名片卡或记忆摘要卡，支持「人物 · 记忆」精简视图与「全量五层」切换、关键词检索直达、按群域筛选与 `3D / 2D` 切换。
  - **记忆图谱增强与直接管理**：`GET /api/memory/graph` 补充 `include_archived`、人物关联记忆计数及回忆次数元数据；新增 `PATCH /api/memory/episodes/{id}` 与 `DELETE /api/memory/episodes/{id}`，支持在详情抽屉中直接标星纪念日、归档或删除记忆。
  - **全站深海观测站主题与防缓存**：统一雾海浅色 / 深海夜幕双主题，侧边栏替换为统一尺寸线性 SVG 图标，并为 `/` 与 `/static/*` 注入 `Cache-Control: no-cache` 响应头，彻底消除更新后的旧样式缓存错乱。
- **在线更新体验与容错加固**：
  - 宿主机更新器 `host_updater.py` 在更新前预检兼容手动重启后 `/healthz` 缺少 `commit` 的场景（更新后健康检查仍严格校验新提交哈希）。
  - WebUI 更新流程新增 `sessionStorage` 持久化任务追踪与后台轮询，容器重启期间自动静默等待恢复，更新完成后自动刷新网页；更新历史表默认精简展示最近 5 条记录及短哈希。
- **表情包本地缓存代拉与夜间反思自动审核**：
  - `AutoStickerReactionPlugin` 采集群图时落盘保存 `image_bytes`，新增 `GET /api/stickers/{id}/image` 优先读取本地缓存并在缺失时无 Referer 代拉回填，彻底解决待审核表情缩略图因 QQ 防盗链/`rkey` 过期不显示的问题。
  - `ReflectionService.run_nightly_reflection()` 新增 `_review_pending_stickers()`，在凌晨反思与手动反思阶段自动调用反思模型（含规则兜底）审核待审表情包、过滤报错截图/二维码噪声并润色中文标签。

## v1.9.2 (2026-11-26)
- **群聊/私聊引用回溯与私聊多轮连续性修复**：
  - `SnowLumaAdapter` 捕获取消丢弃的网关真实 `message_id`（HTTP 回执与 WS Echo 均支持）并写入 `OutboxDelivery.onebot_message_id`；`CompanionPipeline` 按每个实际发送成功的气泡分别写入 `ChatMessage`（保留 `request_id` 关联整轮，同时覆盖搜索过渡语与语音回复），发送失败的气泡不再写入已发言记录。
  - `ChatMessage` 新增 `peer_qq` 与 `request_id` 字段（含幂等迁移 `db/migrations_v1_5.sql`），修复私聊 L1 历史查询丢失机器人上一句及私聊引用机器人回复查不到原文的问题。
- **门卫自学习口径修正与反思隔离/长期记忆修复**：
  - `OutboxDelivery.feedback_status` 默认值改为 `none`，并在迁移中清理升级前缺少 `gatekeeper_action` 的历史挂起记录；`_finalize_cold_drops` 排除 `gatekeeper_action IS NULL`，`_tune_group_thresholds` 仅统计主动插话（`soft_entry`），避免 `direct_pass` 自然结束对话拉高主动接话门槛。
  - 夜间反思改为群聊/私聊分场景独立提取，由程序强制写入 `scene`/`scope` 并校验来源发送者，杜绝私聊线索串入公开画像；增强反思 LLM 与表情打标 JSON 围栏剥离解析并提升 `max_tokens=1200`，为 `MemoryEpisode` 自动补齐 `group_id` 与语义向量 `embedding_json`（兼容非 1536 维向量检索）。
- **表情包发图协议与收图性能优化**：
  - 在 `prompt_builder.py` 公共静态区注入斗图语法与库内高频标签示例，解决自定义人设不触发发图问题。
  - `StickerService.observe_group_image` 改为精确索引查重 + 轻量 `(id, phash)` 扫描，`pick_sticker_for_tag` 下推数据库有界匹配并移除无关标签盲目兜底；`AutoStickerReactionPlugin` 先提交入库再异步后台打标，消除 5 秒超时。

## v1.9.1 (2026-11-26)
- **内存泄漏防护与连接池复用**：
  - `CompanionPipeline._get_group_lock()` 为 `_group_locks` / `_group_generations` / `_group_msg_counters` 增加 `2048` 容量上限，超出时自动淘汰最早的未加锁群组记录。
  - `ModelHubService` 改用持久化 `httpx.AsyncClient` 复用连接池并按请求传入 `timeout`，在应用 `lifespan` 关闭时调用 `aclose()` 优雅释放连接。
  - 修复 `LoginRateLimiter._prune()` / `is_blocked()` 在清理过期 IP 后因 `defaultdict(list)` 再次插入空列表的问题。
- **生产安全性与查询/Schema 加固**：
  - 在 `main.py` 的 `lifespan` 启动流程校验生产环境默认密钥：若 `MASTER_ENCRYPTION_KEY` 或 `JWT_SECRET_KEY` 仍为已知默认值则阻止启动，默认管理员密码打印告警。
  - 显式初始化 `StateStore._event_seq = 0`，消除动态属性依赖。
  - `MemoryService.forget_user_data()` 将时间窗口与 PostgreSQL `JSONB` 包含过滤下推到 SQL 查询中，避免对 `ambient_summaries` 全表扫描。
  - 补全 `db/init.sql` 中 `outbox_deliveries`（`request_id`、`onebot_status`、`status VARCHAR(24)`）与 `usage_metrics`（`source`、`request_id`）缺失字段，并补充回归测试 `tests/test_hardening_fixes.py`。

## v1.9.0 (2026-11-26)
- **群聊拟人化与短链路体验综合修复**：
  - **门卫话题打断衰减与上下文防盲**：`GatekeeperService` 新增同群第三方发言打断检测（`group_last_human`），当热聊窗口（0–60 秒）内有第三方群友发问、发图或引用别人后，自动撤销/惩罚 `0.35` 低门槛并向门卫注入 `[Third-Party Topic Interruption]` 提示；当门卫判定 `other_user` 时同步清理过期个人关注窗口，减少误插话。
  - **异步图片摘要与门卫/L1 图片可见性**：`_gatekeeper_model_scorer` 与 `MemoryService.assemble_context` 不再丢弃纯图片消息，统一通过 `ImageVisionCache` 批量解析 `[图片: 摘要]` 或 `[图片]` 占位；支持群聊静默图片在后台单并发（`Semaphore(1)` + `dHash` 去重）异步生成 15–30 字极简摘要并反写缓存，守住 2GB 节点内存与 Token 预算。
  - **锁外拟人拆泡延迟与 `generation_id` 在途取消**：`MessageDebouncer` 默认窗口调优至 `2.2s` 并新增 `max_wait_seconds=6.0` 上限；主脑多泡发送移至群锁外执行并支持 `0.8–1.8s` 动态打字延迟，期间若同群新消息到来或原消息撤回，通过递增 `generation_id` 立即取消剩余过期泡泡。
  - **去 AI 味、自指锚点与 L1 拆泡原样留档**：主脑静态前缀与默认人设明确禁止脱口秀比喻模板并允许 3–15 字平淡短回，写死「AI/机器人/本名即本人、回复当前发言人统一用“你”」自指锚点；新增带白名单（保护 URL、代码块、列表与 >80 字长回复）的 `format_casual_bubbles()` 去除闲聊句尾句号与外层引号；Bot 回复写入 `ChatMessage` 时改用 `"\n".join(sent_bubbles)` 保留分泡换行，并在 L1 上下文以 `[我(bot_name)]` 逐泡展示。
- **受控自我演化、4D 画像与记忆代谢**：
  - 接入 4D 熟人画像结构化契约（`profile_schema`）、群聊隐式社交反馈回填（`engaged`/`rejected` 自动强化 L3 记忆或生成 48h 表达自纠偏提示）、L3 低价值记忆代谢归档，以及意识海修复（过滤 Bot 自身账号出现在群友图谱与身份列表）。

## v1.8.1 (2026-11-26)
- **在线更新检查与三级自动化更新体系**：
  - 修复 `UpdateService.check_for_updates()` 在 GitHub Release 接口返回 404 回退至 `main` 分支提交时调用未定义 `logger` 导致后台返回 HTTP 500 的缺陷，并补充当容器未注入 `APP_GIT_COMMIT` 时自动向宿主机代理 `/check` 查询当前提交的兜底。
  - **第一级（自动放行）**：在 `ops/updater/host_updater.py` 内置零依赖标准库 Compose YAML 解析器与语义 Diff 判定；当 `docker-compose.yml` 仅调整 `companion-core` 的 `environment`（如 `TZ`）、`mem_limit`、`logging`、`healthcheck`、`restart` 等安全字段且未修改挂载卷、端口、网络、`privileged` 或 `postgres`/`redis` 等其他服务时直接自动放行更新。
  - **第二级（WebUI 看 Diff + TOTP 二次确认放行）**：新增宿主机 `POST /inspect` 与后台 `/api/system/update/inspect`、`/api/system/update/confirm-token` 接口及 WebUI Diff 审查弹窗；修改 `Dockerfile`、受保护 Compose 字段或更新器脚本时返回精确 `git diff` 并绑定 `(to_commit, diff_sha256)` 签发 5 分钟一次性 HMAC 确认令牌（已开启 TOTP 时校验 6 位动态码，未开启时校验管理员密码）。
  - **第三级（凌晨无人值守定时补丁更新）**：在 `CoreScheduler` 与 WebUI 新增可开关的定时自动更新策略（默认关闭、可配置凌晨执行时段如 `04:00`），严格仅对第一级安全变更且同小版本补丁（如 `v1.8.x`）自动拉取更新；涉及第二级受保护变更或跨版本升级时自动去重推送告警/QQ 通知等待人工确认。
  - **更新器自身受控升级**：检测到 `ops/updater/host_updater.py` 变更时先备份旧脚本并使用 `python3 -m py_compile` 校验语法，待 Core 健康检查通过后原子覆盖并通过 `systemd`（`Restart=on-failure` + `ReadWritePaths`）平滑重启生效。

## v1.8.0 (2026-11-26)
- **2GB 生产服务器资源调优与运维安全**：
  - `docker-compose.yml` 新增内存上限（`postgres: 512m`、`companion-core: 448m`、`searxng: 192m`、`redis: 64m`、`caddy: 64m`、`pg-backup: 128m`）、PostgreSQL 参数调优（`shared_buffers=128MB`、`work_mem=4MB`、`max_connections=40`）、Redis `--maxmemory 48mb --maxmemory-policy allkeys-lru`，及全容器 `json-file` 日志轮转（`max-size=10m`, `max-file=3`）。注：若通过在线更新部署，受宿主机保护机制限制（`Dockerfile`、`docker-compose.yml` 为受保护文件），需手动在宿主机同步 Compose 并重建容器。
  - 拆分 `requirements.txt` 与 `requirements-dev.txt`，生产镜像剔除测试依赖。
- **版本化数据库迁移与后端模块化**：
  - `DatabaseManager` 新增带校验和追踪的 `schema_migrations` 迁移表机制（`apply_versioned_migrations`）及 `db/migrations_v1_3.sql`（补充 `admin_users.token_version`）。
  - 从 `brain_service.py` 拆分 `prompt_builder.py` 与 `search_orchestrator.py`，从 `model_hub.py` 拆分 `circuit_breaker.py`；清理后端多处静默 `except Exception: pass` 异常吞没并补充结构化警告/调试日志。
- **前端原生 ES Module 零构建拆分与彻底防 XSS**：
  - 移除所有 `localStorage` JWT 存储，统一采用 `HttpOnly` Cookie + `credentials: 'same-origin'` + `/api/auth/me` 会话恢复 + 服务端 `/api/auth/logout` 撤销令牌版本。
  - 将 `frontend/index.html` 样式与脚本拆分为 `frontend/css/app.css` 与 `frontend/js/*.js`（零 Node/构建链），移除动态 HTML 拼接与内联 `onclick` 字符串拼装，改为原生 DOM 构建与 `data-action` 事件委托，并在运维页新增快照恢复与预检（Dry-Run）入口。

## v1.7.0 (2026-11-26)
- **群聊爆发防抖合批与同群串行互斥**：将 `MessageDebouncer`（默认 `1.5s` 窗口，支持撤回前摘除与停机排空）接入 SnowLuma WebSocket 入站路径（私聊 / `@机器人` / 引用回复立即直通，普通群聊连发自动合批）；在 `CompanionPipeline.process_envelope` 引入按 `group_id` 隔离的异步锁防止同群并发回复乱序或竞态。
- **多模态看图闭环**：将 `MultimodalService` 正式接入主脑流程，仅在 `direct_pass` 强唤醒或已有 dHash/URL 视觉缓存命中时读取图片（支持 GIF 3×3 拼图与缓存反写），避免闲聊图片消耗额外视觉 Token。
- **L3 情景记忆混合检索与快照恢复**：`MemoryService.assemble_context()` 新增基于 PostgreSQL `tsvector` + CJK 二元分词词法 + 向量余弦 + 重要度/时间衰减的 RRF 混合重排（SQL 层面严格维持群/私聊半隔离不变），并在 `main_brain` trace 记录召回条数；新增 `BackupService.verify_and_restore()` 与 `POST /api/ops/backups/restore` 事务级快照校验与恢复接口。

## v1.6.0 (2026-11-26)
- **后端核心缺陷修复与安全加固**：
  - 修复 `CompanionPipeline` 并发请求时直接覆写 `GatekeeperService.soft_entry_threshold` 实例属性导致配置串群竞态的问题，改为按请求参数只读传递。
  - `PluginEventBus` 新增 `emit_background()` 并发受控异步派发（主路径不再被外围插件慢阻塞），并将 `error_log` 与 `SnowLumaAdapter.sent_log` 改为 `deque(maxlen=200)` 防止长跑内存膨胀。
  - 修复 `AutoStickerReactionPlugin` 与 `StickerService.import_manual` 使用受 `PYTHONHASHSEED` 影响的 `hash(url)` 的问题，改为安全下载计算真实 64 位感知哈希 `dHash`（离线或拉取失败时确定性回退至 SHA-256 摘要）。
  - 认证链路加固：引入受信任代理识别（仅环回或私网代理读取 `X-Real-IP` / `X-Forwarded-For`）、基于 Redis/内存降级的异步登录失败限流、按 HTTPS 协议动态下发 `Secure` Cookie，以及 `AdminUser.token_version`（改密与登出即时吊销已签发会话）。
  - OneBot Token 校验改用 `hmac.compare_digest` 防计时侧信道；追踪后台 WebSocket 协程并在 `lifespan` 关闭阶段优雅排空；修复 `host_updater.py` 客户端断连 `BrokenPipeError` 崩溃并增加 `/check` 30 秒 TTL 缓存。
- **统一维护调度、Prompt Cache 调优与门卫全量计费**：
  - 新增持久化幂等调度器 `CoreScheduler`（60 秒唤醒周期），接管 `LifeScheduleService`、每日早间简报、凌晨离线反思及定时备份。
  - 重排 `build_prompt_messages()` 为「静态系统提示词+检索准则 -> Few-shot -> 半静态简报/黑话/L2摘要 -> 动态单轮上下文」结构以稳定前缀缓存命中率。
  - `evaluate_gatekeeper_slot()` 补充提取并记录 `completion_tokens` 与输出费用。

## v1.5.4 (2026-11-26)
- **WebUI 全链路日志与记录时间本地化**：新增 `formatLocalTime()` 辅助函数，修复「全链路追踪」「24h 告警通知」「系统在线更新记录」及「定时自动备份上次执行」直接截取 UTC ISO 字符串或硬编码 `UTC` 后缀导致的慢 8 小时显示问题，统一自动转换为浏览器本地时区展示。
- **后端容器与日程/反思/简报/备份时区统一**：新增 `APP_TIMEZONE`（默认 `Asia/Shanghai`）配置与 `local_now()` 工具函数；在 `docker-compose.yml` 与 `.env.example` 为容器注入 `TZ=Asia/Shanghai`；修复作息状态机 `LifeScheduleService`、离线反思 `ReflectionService`、每日简报 `WebPerceptionService` 及快照备份文件命名原按 UTC 时间/日期计算的问题，并为应用及 Uvicorn 日志补充本地时间戳格式。

## v1.5.3 (2026-11-26)
- **仪表盘今日指标本地零点对齐**：修复 `/api/dashboard/overview` 将 `start_of_today` 固定为 UTC 零点导致本地 `00:00–08:00` 时段「今日 Token 消耗」「语音合成字符」与 `today_spend_usd` 未在零点重置、与柱状图当天桶不一致的问题，统一按 `tz_offset_minutes` 对齐调用方本地零点。
- **生产响应耗时剔除已核查样本**：支持通过 `SystemConfig`（`dashboard/latency`）配置 `excluded_usage_ids` 与 `excluded_alert_ids`，在计算平均耗时、`P95` 与慢响应告警数时排除历史异常样本并在仪表盘展示排除条数提示，同时保持 Token 总量与费用统计不变。

## v1.5.2 (2026-11-25)
- **网页读取（`fetch_webpage`）逐跳重定向 SSRF 防护与独立开关**：
  - 将 `WebPerceptionService.fetch_webpage` 由 `follow_redirects=True` 改为禁用自动跳转并逐跳手动解析 `Location`（最多 5 跳），**每次跳转前均重新调用 `_is_public_http_url` 校验目标地址**，拦截跳转至 `127.0.0.1`、内网网段、云元数据地址或 `file://` 等非公网协议的 SSRF 绕过行为。
  - 新增独立配置项 `WEBPAGE_FETCH_ENABLED`（默认 `true`），可在保留 `SearXNG` / `web_search` 联网搜索的前提下随时单独关闭网页读取工具。
  - 在 `tests/test_autonomous_web_search.py` 补充不安全重定向拦截、安全重定向放行、最大跳转限制及独立开关回归测试。

## v1.5.1 (2026-11-25)
- **群聊话题连续性优化（防断聊）**：
  - **单人注意力窗口渐进护栏**：保留 `max_consecutive_group_turns=4` 作为软收紧起点，在用户个人注意力窗口内从第 `4` 轮起每轮渐进抬高有效阈值（`+0.08/轮`，封顶 `0.90`）并继续交由门卫模型语义评估，放宽注意力窗口硬上限至 `max_attention_group_turns=8`，避免正常追问在第 4 轮后被一刀切静音。
  - **群级话题余温窗口（Group Topic Warmth）**：新增 `group_bot_active:{group_id}` 追踪与 `group_topic_warmth_seconds=180.0` / `group_topic_warmth_floor=0.65` 配置，Bot 回复后 3 分钟内其他群友顺着话题接梗时享有渐进式阈值折让，并向门卫模型注入 `[Recent Group Bot Topic]` 上下文提示。
  - **可观测性与表情包哈希修复**：`pipeline_traces` 的 `gatekeeper` 阶段记录脱敏后的 `reason_code`；修复 `hamming_distance` 在比较 `url_` / `manual_` 前缀等长哈希时抛出 `ValueError` 导致 `auto_sticker_reaction` 插件报错的问题。

## v1.5.0 (2026-11-25)
- **大模型自主联网搜索与独立会话异步转述**：
  - `ModelHubService` 新增 OpenAI 兼容 `tools` / `tool_choice` 与 `tool_calls` 解析支持（兼容 OpenCode Go `deepseek-v4.1-flash` 与 `x-opencode-session` 会话隔离，不支持 `tools` 的接口自动降级重试）。
  - 主脑改由大模型自主判断是否调用 `web_search` / `fetch_webpage` 工具；触发搜索时先即时发送简短过渡语避免聊天卡顿，再在独立 `search:<request_id>` 会话中完成检索、正文读取与自然转述（期间若群聊刷屏自动引用原提问）。
  - `WebPerceptionService` 升级为三级免 API 兜底路由：后台 `web_search` 槽位 → 可选 Docker `searxng` 容器（`--profile search`）→ 内置纯 `httpx` 免费检索兜底，并增加公网网页抓取 SSRF 防护。
  - 链路追踪与运维文档同步更新（`web_search` 追踪标签、`ops/searxng/settings.yml` 与 `docs/OPS_UPDATE_GUIDE.md`）。

## v1.4.4 (2026-11-25)
- **公开仓库与通用人设准备**：将 `persona.json` 替换为通用人设 `echosea_v1`（默认角色名 `回声`），统一 WebUI 侧边栏标题与意识海核心节点标识为 `EchoSea`。
- **默认公开更新源与界面精简**：将 `UPDATE_GITHUB_REPO` 默认值设为 `L-LingYi/echosea`，精简「系统在线更新」卡片标题与模式状态显示，移除「本地仓库模式」等内部阶段文案。
- **仪表盘消耗柱状图优化**：修复按小时统计底部标签高度塌陷导致的柱底错位问题；将按天统计窗口扩展为近 30 天并优化刻度与数值抽样显示。
- **文档完善**：重构 `README.md` 与 `docs/OPS_UPDATE_GUIDE.md`，脱敏现网部署路径并补充环境变量、默认账密与安全配置说明。

## v1.4.3 (2026-11-25)
- **深色模式全面修复**：补齐 `color-scheme: dark`、`.btn.secondary`、`.provider-card`、`.modal-box`、`.seg-bar`、`.progress-track`、`.mobile-bottom-nav`、`tbody tr:hover`、`.toast` 与冷却提示框等深色样式。
- **界面中文化**：将能力槽位、主备路由、接口协议、健康状态、全链路追踪阶段/状态、审核与更新状态等英文技术标识替换为中文名称。
- **总消耗柱状统计**：新增 `/api/dashboard/overview` 的 `usage_series`（支持近 7 天按天与最近 24 小时按小时统计，按本地时区对齐）及仪表盘柱状图。
- **品牌图标升级**：将侧边栏与登录页的「呆」字图标替换为 EchoSea 声波 SVG 图标。

## v1.4.2 (2026-11-25)
- **修复表情包缩略图被裁剪显示不全**：将 `.sticker-thumb` 从 `display: grid` + 固定 `138px` 改为 `200px` Flex 居中容器 + `max-width/max-height: 100%` + `object-fit: contain`，完整展示表情包画面与上下配字，并同步适配深色模式卡片底色。

## v1.4.1 (2026-11-25)
- **深色模式（Dark Mode）**：新增完整深色主题 CSS 变量与侧边栏一键切换按钮，支持自动跟随系统主题并持久化至本地。
- **表情包图库改为推荐外链 + 本地文件导入**：移除易 404 的远程直连下载按钮，改为展示公开图库推荐链接与本地 `.json`/`.jsonl` 文件上传导入（`POST /api/stickers/presets/upload`）。
- **阿里云盘备份自动化与配置向导**：新增后台定时自动备份调度器（支持按每 6h/12h/24h/72h 自动备份、自动清理旧本地快照与失败告警），并在 WebUI 中加入分步接入引导（WebDAV/AList 与 OpenAPI 双模式指引）。
- **文档修复**：同步更新 `README.md` 标题与版本号至 `v1.4.1`。

## v1.4.0 (2026-11-25) — EchoSea（回声海）公开版升级
- **品牌更名**：正式更名为 **EchoSea（回声海）**（保留底层数据库与环境变量向后兼容，升级无痛）。
- **公开表情包预设库（WebUI 按需拉取）**：新增 `GET /api/stickers/presets` 与 `POST /api/stickers/presets/import`，在「表情包图库」内置 `emo-visual-data`、`chinese-meme-description-dataset`、`ChineseBQB` 与自定义 JSON/JSONL 源一键按需拉取（自动去重与标签写入，不撑爆 Git 仓库）。
- **阿里云盘异地备份与自定义路径**：新增 `BackupService` 与完整 WebUI 配置入口，支持「阿里云盘 OpenAPI（OAuth2 授权 / Refresh Token）」「阿里云盘 WebDAV / AList」与「本地快照」三种模式，支持自定义远程目录（如 `/EchoSea/backups`）、一键测试连接与真实快照压缩上传。
- **前端缺失配置全面补全**：
  - 「热点简报与日程」新增**热点渠道源管理**（列表、新增、启停、删除、常用中文源预设）。
  - 「人设与回放评测」新增**新建人设版本**表单与**全局/群聊运行参数**可视化配置（对接 `SystemConfig`）。
  - 「运维、插件与更新」新增**管理员安全设置**（修改密码与 TOTP 二步验证绑定入口）。

## v1.3.6 (2026-11-25)
- **语音合成字符数统计**：在 `/api/dashboard/overview` 与分槽位明细 `by_slot`（`unit: "chars"`）中新增 `today_chars` 与 `total_chars`（统计成功合成的 `cleaned_text` Unicode 字符数），并在仪表盘 TTS KPI 卡片与分槽位表格中以字符数展示语音合成消耗。

## v1.3.5 (2026-11-25)
- **纯 Token 消耗统计**：移除仪表盘 KPI 卡片、槽位明细表及模型编辑器弹窗中的美元费用与预算展示；仪表盘聚焦当日 Token 消耗、窗口期总 Token、输入/输出/缓存 Token 明细及 Prompt Cache 命中率（`daily_budget_usd` 默认设为 `0.0` 即不启用预算限制）。

## v1.3.4 (2026-11-25)
- **TypeSafe AI Jev（`typesafe_systemone`）官方协议适配**：
  - 将门卫判定与健康探针请求体从非标准的 `enum`/`boolean` 切换为官方 TypeSafe System One 的 `choice`（含 `criteria` 映射）与 `noul` 原语。
  - 更新 `_parse_jev_response()` 以解析官方 `answers.target.choice`、`confidence` 与 `answers.should_chime_in.noul` 字段（同时保留向后兼容）。
  - 规范化 System One 接口 URL 拼接，确保 `https://api.typesafe.ai` 与 OpenCode Zen `https://opencode.ai/zen/v1` 均可正确解析，避免重复 `/v1`。
- **智能故障转移熔断器与自动恢复**：
  - 为所有能力槽位（`chat_complete` 与 `evaluate_gatekeeper_slot`）接入错误感知冷却路由：遇到 HTTP `429`/`402` 配额耗尽时立即将 `primary` 置入冷却期，后续流量以 0ms 延迟直接路由至 `fallback`。
  - 支持解析 `Retry-After` 响应头、`extra_config.quota_cooldown_seconds`（默认 1 小时起并指数退避至最高 6 小时）以及 `extra_config.quota_reset_utc_hour`（适配每日定时重置的免费额度）。
  - 半开自动恢复：冷却期结束后，下一次请求自动探测 `primary`，返回 HTTP 200 即无缝恢复主路。
  - 新增 `POST /api/providers/{id}/reset-cooldown` 接口与 WebUI 冷却状态徽章、一键「恢复主路」按钮。

## v1.3.3 (2026-11-25)（全量累计 Git 发布包）
### 新增
- **随时间衰减的单人专注关注度（Time-Decaying Attention Window）**：
  - 群友通过 `@机器人`、直呼小名或引用机器人进行强唤醒后，系统按 `(群号, QQ号)` 锁定关注度：**0–60 秒（1 分钟）热聊期**门槛降至 `0.35`（后续搭话无需每次 `@` 或引用）；**60–240 秒（1–4 分钟）衰减期**门槛平滑线性回升至 `0.78`；超过 4 分钟自然归零。
  - 若期间该群友 `@他人`、引用他人消息或说出「好了 / 没事了 / 退下」等结束语，关注度立即清零；同群其他人的聊天完全不受干扰。

### 变更
- **分层超时策略**：`direct_pass`（直接呼唤/私聊）绝不丢弃、超理想耗时仅仪表盘告警；门卫设置 18 秒安全降级上限（防 120 秒死等并记告警）；仅对严重过时的主动插嘴 `soft_entry`（耗时 ≥35 秒且已被新消息刷过 ≥5 条）执行静默放弃。

## v1.3.0 – v1.3.2 (2026-11-25)（已全部包含在本全量包内）
- 非阻塞 WS 派发、智能按需引用、门卫最近 5 条上下文归属感知、主脑 `[发言人归属锚点]`、机器人自身回复入库 L1、表情包语境自动打标、慢响应仪表盘告警与 WebUI 全面视觉精修。

## v1.2.0 (2026-11-25)
- 管理后台新增「系统在线更新」，支持检查受控 Git 更新源、安装版本和查看审计记录。
- 独立宿主机服务使用 HMAC-SHA256、60 秒有效期和持久任务去重。业务容器只访问签名接口。
- 更新前备份 PostgreSQL 并保留旧镜像。健康检测核对实际运行提交，失败时恢复上一提交和旧镜像。
- 保留现网 SnowLuma 令牌、网络覆盖配置和豆包 V3 合成。新增 PostgreSQL 更新审计表。

## v1.1.1 (2026-11-25)
- 合并「记忆意识海」菜单，增加 HTML 转义和模型路由 PATCH 编辑，保留未修改的模型额外配置。
- 按合成、提交及受理记录语音状态。现网 PostgreSQL 迁移保留完整脚本执行。

## v1.1.0 (2026-11-25)
### 修复（基于线上运行诊断 `20261125`）
- **数据库 Schema 对齐与老库无损迁移**：在 `db/init.sql` 补齐 `memory_episodes.embedding_json` 字段，新增幂等迁移脚本 `db/migrations_v1_1.sql` 并在 PostgreSQL 启动时自动安全执行，彻底消除新老库字段差异。
- **探针流量与生产指标彻底隔离**：为 `usage_metrics` 新增 `source`（`production` / `probe` / `sandbox` / `reflection`）字段；仪表盘延迟、P95 TTFT 与统计指标自动排除健康探针样本，并在真实调用成功时自动刷新 `last_health_status = 'healthy'`（解决模型探针长期显示 `unknown` 的问题）。
- **TTS 合成与 OneBot 受理状态拆分**：在 `tts_deliveries` 中拆分 `synthesis_status`（音频合成）与 `onebot_status`（OneBot 受理回执），并关联 `request_id` 与 `onebot_echo`。

### 新增
- **统一 `request_id` 全链路脱敏追踪**：新增 `pipeline_traces` 表与 `/api/dashboard/traces` 接口，用同一 `request_id` 串联 `inbound -> gatekeeper -> main_brain -> tts -> onebot` 各阶段耗时与状态，运维日志绝不打印聊天正文。
- **TypeSafe AI Jev 原生门卫协议支持**：在模型总控中心新增 `typesafe_systemone` 协议（原生调用 `POST /v1/systemone` 的 `state` + `questions` 结构并解析校准概率），同时将 `gatekeeper` 槽位正式接入主链路（支持 OpenAI 兼容小模型作为备用路由）。
- **门卫与语音意图标注回归评测**：新增 `POST /api/persona/sandbox/benchmark`，支持使用标注群聊样本批量统计误接（FP）、漏接（FN）与语音意图误判率。
- **意识海（Consciousness Sea）记忆可视化图谱**：新增 `GET /api/memory/graph` 与 WebUI 交互式星海力导向图，直观呈现 L5 自我核心、群聊域、L4 熟人卡、L2 边缘旁听、L3 群情景与私聊半隔离边界、L4 群黑话的关联脉络。
- **WebUI 全面美化与手机端适配**：新增移动端顶部栏、抽屉菜单、底部快捷导航栏、响应式 2×2 KPI 卡片、横向滚动表格与触控手势支持。
