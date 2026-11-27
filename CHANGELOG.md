# 变更记录

## 0.3.0

Windows 桌面客户端（`winclient/`），以及为它配套的一处服务端改动和一轮评审修复。

- 加：**桌面客户端**（tkinter + ctypes，零第三方依赖）。托盘常驻、命名互斥单实例；
  五页：仪表盘（三档额度环 / 池内最坏点 / 冷却倒计时）、账号（加号 / 换 key / 暂停 / 删除 /
  清冷却，服务没起来时直接改配置）、接驳（base_url + token + 五种片段，一键写 Codex
  `config.toml` 与用户环境变量并能还原）、请求（`requests.jsonl` 尾部 512KB 筛选 +
  P50/P95 + `relay.log` 控制台）、设置（客户端偏好与中转参数分区，`check` 诊断）；
  托管 `python -m opencode_pool serve`（外部已在跑的实例只 attach、不杀）；
  `build_exe.ps1` 可打 PyInstaller 包（exe 不含内核，仍需外部 Python + 项目目录）
- 加：`POST /v1/pool/shutdown`（**仅 loopback**）。Windows 上 `terminate()` 是
  TerminateProcess，`serve()` 的 `finally` 不执行；客户端停止前先请服务端自己收尾，
  保住内存里最后几秒的用量/冷却状态。远程面板即使开了 `allow_remote_panel` 也用不了它
- 修（评审发现的真实缺陷，均补了回归用例）：
  - Codex 配置写入把顶层键追加到文件末尾 → 落进最后一张表，`model_provider` 静默不生效；
    且用户原有的 `model_provider` 会与新键重复（TOML 语法错）。现在顶层键替换、
    表段整段替换，写前用 `tomllib` 自校验，写盘原子替换并保留 CRLF
  - 环境变量写入改为"先落还原点再动注册表"，还原按原类型（`REG_EXPAND_SZ` 不再被写成
    字面量），中途失败按原值回滚
  - 进程托管加互斥锁：启动等待中按"停止"不再被静默忽略，也没有第二个进程变成孤儿的路径；
    主动停止/重启不再触发"中转进程退出了"假告警
  - 离线"暂停"同时同步 `pool_state.json`（否则下次启动被 state 里的 `enabled` 覆盖回去）
  - 密钥仓解析失败不再当成"空仓"（一次瞬时读失败会清空所有槽位）
  - 请求日志"协议"列按路径后缀判（`/v1/chat/completions` 原来显示为空）；尾部读只在
    真的截断半行时才丢第一行
  - 试探性细节：`find_python` 在打包版不再拿 exe 当中转解释器、`build_exe.ps1` 的
    `@args` 笔误、监听地址改动改为提示重启（热重载不重绑端口）、离线轮询不再每秒打
    `/healthz`、环境变量面板不再明文显示 token
- 工程：CI 把 `winclient` 纳入 compileall / ruff，并在 Python 3.12 上跑客户端自检
  （没有 tkinter 的 runner 自动跳过 GUI 用例：55 项 → 48 通过 / 7 跳过 / 0 失败）

## 0.2.2

一台机器跑中转、另一台用 DSH 面板的部署（远程面板）。

- 加：`allow_remote_panel`（默认 `false`）。面板能加号、换 key、删号，等同管理台，所以
  远程访问是**显式 opt-in**：开启后，非 loopback 客户端仍必须带对 `relay_token`
  （DSH 插件在宿主侧注入 Bearer）；`relay_token` 为空时**一律拒绝**（否则等于把管理台
  挂到内网上）。
- 修：面板的 403 判据从「硬编码 client_address 白名单」改成 `_panel_authorized()`，
  与 `/metrics` 的鉴权口径一致，便于后续加来源限制
- 测试：新增 5 条（loopback 恒可、默认拒绝远程、开启+对 token 放行、错 token 拒、空 token 拒）
- 发布：npm 上占住 **`opencode-pool`** 这个名字，发布**本体**（`opencode_pool/` + 文档 +
  插件源码快照，44 文件 / 121.8 kB）；DSH 插件仍是 `dsh-plugin-opencode-pool`。仓库根新增
  `package.json`
- 工程：新增 `.github/workflows/release.yml` —— 推 `v*` tag 即自动发布到 npm
  （version 三方校验、发布前回归、`npm pack --dry-run` 留清单、`--provenance`、
  同版本已存在则跳过、自动建 GitHub Release）；`docs/releasing.md` 写清 token 怎么给
- 文档：`docs/configuration.md` 增字段说明；`SECURITY.md` 的「面板仅 loopback」改写为默认仅 loopback；
  `docs/troubleshooting.md` 增「插件页显示没有声明组合包（装错目录）」一节

## 0.2.1

过载下的排队与容量修正。触发场景：2026-11-11 上午峰值 14 并发、可用号只剩 2～3 个时，
客户端成片收到 `queue_ms = 60s/90s` 的 503，`requests.jsonl` 里
`error_reason=concurrency-timeout` 占满整行。

- 修：**排队预算改为「整个请求一个期限」**。`queue_wait_seconds` 以前只管"全池冷却"，
  并发位 / 最小间隔没空走的是 `acquire()` 的签名默认值 30s——**每个号各等 30s**，
  三个号串行 90s，而客户端 60s 就走了。现在容量等待和冷却等待共用一个期限，
  到期才 503（`error_reason` 区分 `concurrency-timeout` 与 `queue-timeout`）
- 修：**容量等待不再当成换号**。并发位没空不是"这个号坏了"：不再记 `failovers`、
  不再解钉、不再把该号踢出本次请求的候选（以前踢掉之后即使位子马上空出来也不会回头用，
  只会一路换到 503）
- 修：**429 退避不再占着并发位睡觉**。退避秒数由 `_attempt` 返回，调用方在释放并发位
  之后才睡（以前 2/4/8s 的睡眠期间该号唯一的并发位是空的、却没人在用）
- 修：**客户端断开后立刻收手**。排队 / 等位期间 peek 连接（`MSG_PEEK`，不消费数据），
  对端已关闭就不再排队、也不再为死连接打一次上游（白烧额度，还把坑位占着）
- 修：**网络层抖动先同号重试一次**（`net:ReadTimeout` / 连接重置），连续两次才冷却 20s，
  免得一次抖动把健康号按停
- 修：`冷却 0 分钟` 文案 —— 小于 60s 的冷却以前被印成 0 分钟（15s 的限流冷却、2s 的测试冷却）
- 修：`scripts/stats.py` 的回归测试写死了 2026-11-06 的时间戳，过了那天起必然红；
  改为相对当前时间生成（测试自己腐烂比 bug 更坑）
- 加：`scripts/load-smoke.py` —— 在本地假上游上跑真 relay 验证过载排队：
  容量够 / 容量不够（失败必须受预算约束）/ 客户端排队中途断开（一次都不该打上游）；
  它是 0.2.1 这轮问题的复现器，改排队逻辑前后各跑一次就能看出差别
- 加：两条回归用例 —— 并发位全满时必须受 `queue_wait_seconds` 约束（不出现 60/90s 串行等待）、
  `_attempt` 遇到 429 只返回退避秒数不在方法内睡
- 文档：`queue_wait_seconds` 语义改写（一个全局期限），排障页补"容量不足"的判据
  （`queue_ms ≈ 60s` 成片 503 → 调 `max_inflight` 或加号，别去调 `queue_wait_seconds`）；
  新增 `docs/verification-20261111.md` 记录本次事故的时间线、根因与压测数据
- 实测参数：6 并发小请求在 `max_inflight=1` 下总耗时 43.0s、改成 2 后 14.0s、3 时 9 并发
  15.7s 且上游 0 个 429；4 时瓶颈已回到上游。本机生产配置定为 **3**（配置项，不是默认值）

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
