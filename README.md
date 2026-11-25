# EchoSea（回声海） v1.4.2（陪伴系统 · 完整三期全功能生产版）

面向 Linux / Docker 云端 24 小时长期运行、群聊优先兼顾私聊的独立拟人陪伴系统，原生适配 **SnowLuma（OneBot v11）** 网关，完整覆盖规格书全部 10 大子系统与三期路线图。

v1.2.0 在线更新使用宿主机守护进程和受控 Git 更新源。现有部署的启用步骤见 [运维更新指南](docs/OPS_UPDATE_GUIDE.md)。

## 完整功能矩阵（一期 + 二期 + 三期）

1. **SnowLuma OneBot v11 接入与 QQ 强绑定**：支持反向 WS（`/ws/onebot/v11`）、正向 WS 客户端自愈与 HTTP 回调；QQ 号唯一主键同步群名片与 `known_names`；引用自动展开、撤回即时拦截、Outbox 审计、单群/全局防风控限流。
2. **三层归属门卫 + 降权软切入**：硬规则识别 `@他人`、`@自己`、准确引用与不 `@` 直呼小名别名；区分直接呼唤与第三人称议论；Redis 活跃互聊防插嘴；降权软切入与连续轮数空气护栏；取消随机概率。
3. **五层深度记忆（含边缘旁听与半隔离）**：PostgreSQL 17 + pgvector 底座；L1 工作记忆 + 30m 群转私聊承接；L2 边缘旁听摘要；L3 情景与纪念日；L4 熟人卡 SQL 级群/私聊严格半隔离 + 群黑话审核；L5 自我状态；一键忘却连根清理。
4. **原生多模态看图与去 AI 味**：dHash 感知哈希旧图缓存 + 3×3 GIF 抽帧拼图；Prompt Cache 静态前缀布局；禁词黑名单、去 Markdown 噪点与保护引文自然拆句发送。
5. **表情包自学习与图库**：感知哈希去重、群内高频与正向反应（哈哈/草/偷了）自动偷图、手工批量导入、WebUI 图库审核以及回复 `[sticker:tag]` 自动斗图。
6. **豆包 TTS 语音管线**：「用语音回答」「引用后说用语音」「别发语音」三类指令识别；默认整段合并合成；口播前清洗 Markdown/链接/括号动作；情绪语速映射；时长上限保护与超时自动降级文字；仪表盘记录合成成功率。
7. **凌晨离线反思引擎**：从 L1/L2 群聊流自动提炼群友公开画像兴趣、发现新黑话（待审队列）及互动纪念日。
8. **互联网热点双通道感知**：慢通道抓取 RSS/JSON 信源提炼“今日刷网简报”融入聊天常识（绝不群发新闻）；快通道聊到新梗/热搜按需联网搜索并用自己口吻转述。
9. **角色作息日程状态机与标准插件生态**：24h 作息日程驱动 L5 精力/心情/活动；`CompanionPlugin` 标准 SDK 与异步事件总线（独立超时与异常隔离），内置自动偷图与异地备份同步告警插件。
10. **模型总控中心与云端 24h 运维**：6 大槽位主备无缝切换、API Key AES-256-GCM 加密存库、一键测活、管理员登录 + TOTP 2FA、多维仪表盘、Token/语音字符消耗统计、阿里云盘定时自动备份、深色模式、多渠道告警与群聊回放沙盒。

## 快速部署（Linux + Docker）

```bash
cp .env.example .env
# 编辑 .env，替换数据库密码、加密主密钥、JWT 密钥与 SnowLuma Token
docker compose --profile https --profile backup up -d --build
```

## SnowLuma 网关对接

- **反向 WebSocket（推荐）**：在 SnowLuma 侧配置反向 WS 地址 `ws://<服务器IP>:8000/ws/onebot/v11?access_token=<SNOWLUMA_ACCESS_TOKEN>`。
- **正向 WebSocket**：在 `.env` 设置 `SNOWLUMA_MODE=forward_ws` 与 `SNOWLUMA_WS_URL=ws://snowluma:3001/`。

## 运行全量自动化测试

```bash
pip install -r requirements.txt
PYTHONPATH=backend python3 -m pytest -v
```
