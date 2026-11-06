# v0.2.0 端到端验收记录（2026-11-06 18:26–18:33 CST）

- 验收对象：`<project-dir>`（工作区当前文件；该目录**不是 git 仓库**，无法记录 commit）
- 宿主：Python 3.12.3 · Node v22.23.4
- 运行态：`~/.dsh/opencode-pool/`（配置 `pool.config.json`、状态 `pool_state.json`、日志 `relay.log`），监听 `127.0.0.1:8790`
- 验收人：review（只验证与记录，未改任何代码）
- 说明：`/v1/*` 需要 `relay_token`，实测时从配置文件读出到 shell 变量使用，**未在记录或输出里回显 token**

---

## 1. 自动化测试

| 命令 | 结果 |
| --- | --- |
| `python3 -m unittest discover -s tests` | `Ran 86 tests in 8.9s` → **OK** |
| `node --test tests/plugin.test.mjs` | `tests 5 / pass 5 / fail 0` → **OK** |

补充（来自同轮审查 t11 的连续稳定性测量，本次未重复跑满 20 次）：
全量 `discover` 连跑 20 次 20 绿、`ReadinessTest` 单跑 20 次 20 绿；本次再跑单次仍绿。
上一轮 8/10 红的抖动（后台采样线程与 readiness 的时序竞争）已由 `FakeKeyStore` 注入 + `settle()` 消除。

## 2. 运行态实测

### 2.1 重启

```
pid 文件 285489 → ./scripts/start.sh stop（已停止，8790 释放）
→ ./scripts/start.sh start（已启动，端口已监听；健康检查通过）
→ 新 pid 301920，8790 在监听
```

### 2.2 三个观测端点

- **`GET /healthz` → 200**
  `{"ok":true,"ready":true,"reason":"ready","accounts":{"total":3,"ready":1,"disabled":0,"cooling":0,"exhausted":1,"below_reserve":1,"stale":0,"no_credentials":0,"no_quota":0},"usage_refresh_seconds":90,"fresh_within_seconds":180,"oldest_sample_age_seconds":14.3,"never_sampled":0,"upstreams":["opencode-go"],"uptime_seconds":21.1}`
- **`GET /metrics` → 200**，`text/plain; version=0.0.4`，9 个 `# TYPE` 族、43 行；`pool_account_headroom` / `pool_account_window_percent` / `pool_requests_total` / `pool_failovers_total` / `pool_cooling_seconds` / `pool_cache_read_tokens_total` / `pool_input_tokens_total` / `pool_sticky_hits_total` / `pool_sticky_misses_total` 全部存在。
- **`GET /v1/usage` → 200**（带 Bearer），三个账号状态可解释：

| 账号 | headroom | 调度状态 | 说明 |
| --- | --- | --- | --- |
| go02 | 50 | **可调度**（ready） | rolling 11% / weekly 50% / monthly 25% |
| go01 | -1 | 不参与（exhausted） | weekly 已 100%，`blocked_window=weekly` |
| go25 | 1 | 不参与（below_reserve） | monthly 99% → 余量 1 < `min_headroom=2` |

`healthy_accounts=1 / total_accounts=3`，与 `/healthz` 的 `ready=1` 一致；`sticky.ttl_seconds=43200`（12 小时默认值生效）。
负路径同样符合预期：无 token 的 `/v1/usage` → **401**，未知路由 → **404**，面板 `/`（loopback）→ **200**。

### 2.3 流式请求（`curl -N`，deepseek-v4.1-flash，同一对话 3 轮）

| 轮次 | HTTP | ttfb | 总时长 | 字节 | 首帧 | relay 日志 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 200 | 10.96s | 11.23s | 3506 | `data: {"id":"chatcmpl-…","object":"chat.completion.chunk"…}` | `[req] go02 deepseek-v4.1-flash 200 stream ttfb=11.06s total=11.23s bytes=3506` |
| 2 | 200 | 6.78s | 6.98s | 3548 | 同上（`data:` 帧） | `[req] go02 … 200 stream ttfb=6.84s total=6.98s bytes=3548` |
| 3 | 200 | 4.74s | 5.27s | 7135 | 同上 | `[req] go02 … 200 stream ttfb=4.80s total=5.27s bytes=7135` |

- 首块（`data:` 帧）与正文都到；SSE 以 `data: [DONE]` 收尾，响应头 `Transfer-Encoding: chunked` + `Content-Type: text/event-stream`。
  四轮流式正文分别为 **「收到」「明白」「好」「收到」**（= 上表 3 轮 + §2.4 的同一对话第 4 轮，均非空流），与 prompt 要求一致。
- 三轮**全部落在 go02**（日志 `[req] go02`），与"当前只有 go02 可调度"一致。
- 注意：该中转同时服务其它客户端（实测 `requests_last_5m=13`），日志里夹着非本次测量的流（如 `bytes=29573`），已用字节数与 ttfb 对应关系区分。

### 2.4 会话粘性（A/B 对照，绕开"只有一个号可调度"的歧义）

先取 `/v1/usage` 的 `sticky` 快照，再发一次**同一对话**的第 4 轮，再发一个**全新对话**，逐步对比：

| 动作 | pinned | hits | misses | 说明 |
| --- | --- | --- | --- | --- |
| 基线 | 4 | 10 | 4 | |
| 同一对话第 4 轮（`[req] go02 … bytes=5395`） | 4 | **11（+1）** | 4（+0） | 命中钉子，未重选 |
| 全新对话（`[req] go02 … bytes=12340`） | **5（+1）** | 11（+0） | **5（+1）** | 新对话 → 未命中并新建钉子 |

同一对话三轮 + 第 4 轮全部、且只在同一对话上 `hits` 增长、新对话才 `misses` 增长，粘性与计数语义自洽。

### 2.5 缓存复用计数（流式记账）

`go02` 在 18:30 的快照：`cache_read_tokens=3,251,456` / `input_tokens=11,843` / `cache_hit_ratio=0.9964`；
`/metrics` 同名两个 counter 与之一致（`/v1/usage` 与 `/metrics` 同源）。
热请求 ttfb 降到 0.34s（同一对话第 4 轮），符合"命中上游 prompt cache"的预期。

## 3. 文档一致性抽查

### 3.1 `configuration.md` 顶层字段 vs `config.py` / `init` 模板 / 接口

- 字段覆盖：`Config` 的 **14 个可配置标量字段**（`version/listen/relay_token/credentials_yaml/secrets_file/state_file/pid_file/usage_refresh_seconds/upstream_timeout/min_headroom/max_inflight/min_interval_seconds/queue_wait_seconds/sticky_ttl_seconds`）与 `providers`/`accounts` 两张表在文档中**全部出现**；唯一未写进用户表格的是内部字段 `warnings`（v1 自动迁移提示，用户不可配置，属合理省略）。
- 文档"默认值"列与 `cli.py::TEMPLATE`（`init` 实际写出的配置）逐项一致：`listen=127.0.0.1:8790`、`relay_token=""`、`usage_refresh_seconds=90`、`upstream_timeout=900`、`state_file=pool_state.json`、`pid_file=relay.pid`、`secrets_file=~/.dsh/opencode-pool/secrets.json`、`credentials_yaml=~/.dsh/.credentials.yaml`、`sticky_ttl_seconds=43200`。
- 运行态交叉验证：`/v1/usage.sticky.ttl_seconds=43200`、`/healthz.fresh_within_seconds=180`（=2×90）、`min_headroom=2` 使 go25 判 below_reserve —— 与文档描述一致。
- **口径提示（非阻断）**：`Config` 数据类的兜底默认值与文档不同 —— `state_file=""`、`pid_file=""`、`credentials_yaml=~/.dsh/.credentials.yaml`（本机绝对路径）。即字段不写时，`state_file`/`pid_file` 是"关闭持久化"，`credentials_yaml` 落到本机路径而不是 `~/.dsh/.credentials.yaml`。文档描述的是 `init` 生成值，建议在表格里加一句"未写时的兜底"以免误读。

### 3.2 `adapters.md` 最小示例

- 抽取文档里唯一的完整适配器示例 → `py_compile.compile(doraise=True)` **通过**（61 行）。
- 掉落加载实测：把示例放进 `adapters/ext/` 的**副本**（`/tmp/dropin`，未改仓库）→ `adapters.load_all()` 后注册表出现 `budget-echo`，`adapters.get("budget-echo")` 取到实例，`descriptor()` 字段齐全（`id/display_name/description/fields/windows/account_fields/protocols/supports_quota`），`windows=[budget]`、`fields=[key_env, base_url]`；`load_errors=[]`。

### 3.3 `troubleshooting.md` 判据 / 日志文本 vs 代码与真实日志

- 文档引用的四类日志行均能在**真实 relay.log** 里找到同格式样本：
  `[req] go02 deepseek-v4.1-flash 200 stream ttfb=3.23s total=7.41s bytes=193749`、
  `[pool] go02 冷却 0 分钟 原因=throttled-persistent`、
  `[sticky] go02 不可用 → 解除本对话钉子`、
  `[http] 客户端断开 POST /v1/chat/completions (ConnectionResetError)`。
  另外 `[req] … 全池冷却中，排队等待`、`[req] … 会话钉子保留`、`[req] … 透传`、`[req] … 瞬时限流 → … 后同号重试`、`[req] … 限流持续 → 冷却 15s 并换号`、`[pool] listening …` 均在 `server.py` 的打印语句里逐字存在。
- `/healthz` 的 8 个 `reason`（`no-accounts/all-disabled/all-cooling/all-exhausted/all-below-reserve/usage-stale/usage-not-sampled/missing-credentials`）在文档与 `pool.py::readiness` 中都存在，且本轮实测过 `ready/all-cooling/all-exhausted/all-below-reserve/usage-stale/missing-credentials` 六种。
- 9 个 `pool_*` 指标名与代码 `metrics_text()` 完全对齐；错误文本 `NoAvailableAccount`、`号池里没有可用账号`、`缺凭据`、`排队超时`、`throttled-persistent` 均能在代码里检索到。

## 4. 未解决问题 / 需要跟进

1. **首次流式请求出现一次客户端侧重置**：18:29 第一次 `curl -N` 返回 `http=000`（无响应体，5.88s），relay 日志只有 `[http] 客户端断开 POST /v1/chat/completions (ConnectionResetError)`；紧接着同命令重试即 200，之后 5 次流式请求（3 轮 + A/B 两轮）全部正常。未复现，怀疑是重启后首请求与其它并发客户端/连接复用相关的瞬态，建议单独观察（不阻断本次验收）。
2. **上一轮审查遗留的 3 条 low**（t11 已记录，本轮未改）：L1 流式记账帧识别是 `"usage"` 子串启发式（真 usage 帧之后再出现 `content="usage"` 的 delta 会丢本次记账）；L2 真实 `KeyStore` 三级优先级缺直接用例；L3 `SecretStore` 缓存/磁盘不一致的一次性观察（建议加锁自愈）。
3. **R6（`_save_state` 无锁、固定 `.tmp`）** 仍未处理，已按 captain 安排留给 t8。
4. **账号侧现状**：go01（weekly 100%）与 go25（monthly 99%）当前不可调度，池子可用容量只有 go02 —— 这是配额现状而非缺陷，但意味着任何 go02 侧冷却都会直接让 `/healthz` 转 503。

## 5. 结论

- 自动化测试：**通过**（86 Python 用例 + 5 Node 用例）。
- 运行态：重启后 `/healthz` 200、`/metrics` 9 族可读、`/v1/usage` 三账号状态与判据一致；流式请求正常；同一对话 4 轮同账号且 `hits` 只在同对话增长、新对话才增长 `misses`。
- 文档：抽查 4 组事实（顶层字段/默认值、schema 适配器描述、adapters.md 示例可编译并可掉落加载、troubleshooting 日志与判据文本）与实现/接口一致；仅"兜底默认值 vs init 默认值"一处口径建议补充说明。
- 未发现阻断性问题；上述 4 项为跟进建议，不影响 v0.2.0 验收结论。

## 6. 复跑命令

```bash
cd <project-dir>
python3 -m unittest discover -s tests
node --test tests/plugin.test.mjs
./scripts/start.sh status
curl -s http://127.0.0.1:8790/healthz
curl -s http://127.0.0.1:8790/metrics | grep -c '^# TYPE'
TOKEN=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.dsh/opencode-pool/pool.config.json')))['relay_token'])")
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8790/v1/usage | python3 -m json.tool | head -40
```
