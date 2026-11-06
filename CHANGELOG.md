# 变更记录

## 0.2.0

会话粘性、可观测性与工程完备性。

- 加：**会话粘性路由** —— 以对话指纹（model + system + 首条 user）为 key 把同一对话钉在
  同一账号，保住上游 prompt cache；TTL 新配置项 `sticky_ttl_seconds`（默认 43200 /
  12 小时，`0` 关闭）。TTL 只是安全上界，映射表由容量 LRU（`STICKY_MAX = 4096`）
  回收——上游 prompt cache 保留是小时级，TTL 太短会作废仍然有效的缓存。
  账号不可用（冷却 / 额度耗尽 / 低于 `min_headroom`）才重选并更新钉子，
  失败换号后解除钉子
- 加：**`/metrics`** —— Prometheus 文本格式：`pool_account_headroom` /
  `pool_account_window_percent` / `pool_requests_total` / `pool_failovers_total` /
  `pool_cooling_seconds` / `pool_sticky_hits_total` / `pool_sticky_misses_total` /
  `pool_cache_read_tokens_total` / `pool_input_tokens_total`
- 加：**`/healthz` 就绪语义** —— 200 = 至少一个账号可调度且额度采样新鲜
  （年龄 < 2×`usage_refresh_seconds`）；503 = 无可用账号或采样过期，
  body 给原因与计数、不含密钥。Docker HEALTHCHECK 以它为就绪判据；
  `start.sh` 判存活看端口、判可用性看它
- 加：**瞬时限流同号指数退避重试**（2s → 4s → 8s，预算耗尽才冷却换号）与
  **并发 / 速率保护**：每号并发位 `max_inflight`、最小间隔 `min_interval_seconds`、
  全池限流排队 `queue_wait_seconds`（`/v1/usage` 新增 `sticky` 计数
  `ttl_seconds / pinned / hits / misses`，账号条目新增缓存观测
  `cache_read_tokens / input_tokens / cache_hit_ratio`）
- 加：`below_reserve` 标注 —— 余量低于 `min_headroom` 的账号在面板标
  「余量保留中 · 不参与调度」，状态胶囊与寿命带文案一致
- 加：**结构化请求日志** `requests.jsonl`（一行一个 JSON、一个请求一行、21 字段；
  缺失写 `null` 不写 0；`request_log_path / request_log_max_bytes / request_log_keep`
  可配、按大小轮转 `.1 .2 …`）+ 零依赖分析脚本 `scripts/stats.py`
  （成功率 / 延迟分位 / 缓存与粘性命中率 / 按账号 TTFB）。`req_id` 贯穿排队·重试·
  换号，`queue_ms` 归本地、`ttfb_ms` 归上游；日志不含 prompt 正文与 Authorization
- 工程完备性：MIT `LICENSE`、`CONTRIBUTING.md`、`SECURITY.md`（威胁模型）、
  GitHub Actions CI（Python 3.10/3.11/3.12 + Node 20/22 矩阵，不联网、零密钥）、
  `Dockerfile`（非 root、`/data` 挂载点、HEALTHCHECK）+ `docker-compose.yml` +
  `.dockerignore`、`deploy/opencode-pool.service`（systemd）+ 部署手册
  （Linux systemd / Windows 任务计划程序 / Docker）
- 文档体系：`docs/quickstart.md`、`docs/configuration.md`（逐字段参考）、
  `docs/adapters.md`（Adapter 契约 + 最小示例）、`docs/troubleshooting.md`
  （报错判据 / 日志与指标读法）、`docs/analytics.md`（请求日志分析）、
  `docs/verification-20261106.md`（端到端验收记录）；`README.md` 重写（架构图、
  功能表、与 opencode2api / Cline-proxy / gpt-load 的定位差异）

> 已知限制（0.2.0）：见 `docs/analytics.md`「已知限制」——
> F1 非流式取消时人类日志行显示 200（以 `requests.jsonl` 为准）、
> F2 `[http]` 摘要行无 req id（连接级事件）。均计划 0.2.1 修复。

## 0.1.1

- 修：`lib/client.js` 的 `__ModuleLoader__.load({ id })` 写成 cordis 条目 id，
  导致 DSH web boot 停在全有全无的 `Failed to load plugins`。改为 npm 包名，并加断言守住
- 修：CLI 只接受 `--config` 在子命令之前，插件与 `start.sh` 用的 `serve --config X`
  会 `unrecognized arguments` 秒退。现在两个位置都接受
- 加：`tests/plugin.test.mjs` 校验"模块 id == 包名"与"必须走 `__ModuleLoader__.load`"
- 加：`tests/test_opencode_pool.py` 校验 `--config` 两个位置
- 文档：三个 id 的区分、报错判据（`without registering` vs `X: failed`）

## 0.1.0

首个发行版，形态为 **DSH 插件**。

- OpenCode Go 适配器：`/v1/usage` 三档额度、`x-opencode-session` 注入、`GoUsageLimitError` 按窗口冷却
- 池调度：`headroom = min(100-percent)`，超额换号，无监督上游退化为轮询 + 冷却
- 控制台：窗口卡 / 三环仪表 / 寿命带 / 加号·换 key·删号 / 热重载配置
- 配置 v2（provider + account 分离），v1 自动迁移
- 适配器注册表与 `adapters/ext/` 扩展目录
- DSH 插件：宿主侧反向代理（局域网可用）+ 中转进程托管 + 侧边栏面板
- 23 个回归用例覆盖声明/配置/判定/调度/端到端/账号生命周期
