# v0.2.0 最终端到端验收记录（覆盖 t5/t8/t9 当前字节）

- 时间：2026-11-06 19:03–19:22 CST
- 对象：`<project-dir>`（工作区当前文件；非 git 仓库，按 mtime 记录快照）
- 代码快照：`opencode_pool/{pool,server,config,secrets,reqlog}.py` 18:49–18:50、`scripts/stats.py` 18:54、
  `tests/test_opencode_pool.py` 18:54
- 文档快照：`configuration.md` / `docs/analytics.md` 19:07:30、`README.md` / `CHANGELOG.md` 19:07:54
  （**mtime 晚于本任务开始**：t9 的文档是在本验收进行中补上的，见第 7 节说明）
- 宿主：Python 3.12.3 · Node v22.23.4 · 中转 `127.0.0.1:8790`
- 验收人：review（只验证与记录，未改任何代码；`/v1/*` 的 relay token 从配置读入 shell 变量使用，未回显）

---

## 1. 自动化测试

| 命令 | 结果 |
| --- | --- |
| `python3 -m unittest discover -s tests` ×10 | **10/10 OK**（`Ran 111 tests`，单次 14–18s） |
| `node --test tests/plugin.test.mjs` | **tests 5 / pass 5 / fail 0**（53.5ms） |
| `./scripts/verify.sh 20` | **20/20 全量 discover OK + 20/20 ReadinessTest OK + 每文件 OK → `RESULT: STABLE`**（exit 0） |

结论：t5（+6 例）/t8（+10 例）/t9（+9 例）新增用例都在，且套件稳定（本轮独立跑出 10/10 与 40/40 两轮）。

## 2. 运行态（重启后）

- `./scripts/start.sh stop && start`：旧 pid **338742** → 新 pid **357145**，8790 在监听。
  （`start.sh` 启动瞬间打印「/healthz 未 200」，是端口刚监听、首轮额度采样还没落定的 2 秒窗口；
  5.2s 后 `/healthz` 已 200。start.sh 的提示语把这种情形描述成「没有可调度账号」，建议后续区分「采样未完成」。）
- `GET /healthz` → **200**：`reason=ready`，`total=3 / ready=1 / exhausted=1 / below_reserve=1`，
  `fresh_within_seconds=180`、`oldest_sample_age=2.4s`、`no_credentials=0`。
- `GET /metrics` → **200**，`text/plain; version=0.0.4; charset=utf-8`，**9 个 `# TYPE` 族**、23 条样本：
  `pool_account_headroom` / `pool_account_window_percent` / `pool_requests_total` / `pool_failovers_total` /
  `pool_cooling_seconds` / `pool_cache_read_tokens_total` / `pool_input_tokens_total` /
  `pool_sticky_hits_total` / `pool_sticky_misses_total`。
- `GET /v1/usage`（Bearer）→ **200**：`healthy_accounts=1/3`（与 healthz 的 `ready=1` 一致），
  `sticky.ttl_seconds=43200`；go02 `headroom=50` 可调度、go01 `blocked_window=weekly`（周额度 100%）、
  go25 `headroom=1 < min_headroom=2` → `below_reserve`；go02 `cache_hit_ratio=0.997`。

## 3. 日志链路实测

发出的请求（`deepseek-v4.1-flash`，全程用 curl，未改配置）：

| # | 请求 | 结果 | relay.log 新增行 |
| --- | --- | --- | --- |
| 1 | 非流式 | 200 / 1156B / 2.02s | `[req] go02 … 200 total=2.02s bytes=1156 cache=0/35 sticky=miss` |
| 2 | 非流式 | 200 / 1119B / 2.68s | `[req] go02 … 200 total=2.68s bytes=1119 cache=0/35 sticky=miss` |
| 3 | 流式 `-N` | 200 / ttfb 1.32s / 17422B | `[req] go02 … 200 stream ttfb=1.32s total=3.41s bytes=17422 cache=0/35 sticky=miss` |
| 4 | 透传（坏模型） | 400，上游 `Model is unavailable` | `[req] go02 no-such-model-xyz 400 透传 req=84407b11` + 记账行 `400 total=0.31s bytes=92` |
| 5 | 流式，2s 取消 | curl exit 28，收到 0B | `[req] go02 … stream ttfb=3.03s total=3.03s bytes=207 cache=-/- sticky=miss` |
| 6 | 流式，6s 取消 | curl exit 28，收到 70386B | `[req] go02 … stream ttfb=0.93s total=6.02s bytes=70932 …` |
| 7 | 流式，5s 取消 | curl exit 28，收到 52097B | `[req] go02 … stream ttfb=0.87s total=5.02s bytes=52626 …` |

（同期日志里还有其它客户端的大流式请求，`bytes` 十万级、`sticky=hit`，与上表可用字节数/ttfb 区分。）

- **relay.log 无 Traceback**：以重启横幅 `[pool] listening`（第 9825 行）为起点统计 → **0 行 Traceback**。
- 日志中的 `[http] 客户端断开` 摘要：全文件 68 条，类型全部 `ConnectionResetError`，
  形如 `[http] 客户端断开 POST /v1/chat/completions (ConnectionResetError)`；
  但它们来自 **keep-alive 连接被重置**（下一次 readline 失败），**不是**本次的流式取消（见第 5 节）。
- `req=<id>` 对账：日志里 `req=84407b11` ↔ `requests.jsonl` 中同 `req_id` 唯一一行
  （`status=400, upstream_status=400, outcome=passthrough, error_reason=client:400, sticky=miss, bytes=92`）。
- 正常日志形态未变：`[req]` 行仍带 `bytes=`、流式带 `ttfb=`，本轮新增 `cache=<cache_read>/<总输入>` 与
  `sticky=<hit|miss|off>` 两个字段（成功行）。

## 4. `requests.jsonl` 与 `stats.py`

### 4.1 结构化日志

- 文件 `~/.dsh/opencode-pool/requests.jsonl`：**0600**、52 行、25,980 字节。
- **全部行可 `json.loads`**，每行**恰好 21 个字段**：
  `ts, req_id, method, path, model, stream, account, provider, status, upstream_status, outcome,
  queue_ms, ttfb_ms, total_ms, bytes, retries, failovers, sticky, tokens, allowance, error_reason`。
- 本轮（19:06:15–19:08:01）新增 **7 行**：2 非流式 200 + 1 流式 200 + 1 透传 400 + **3 次取消的流式**（均为 `outcome=ok`，见第 5 节）；`tokens` 用真实值或 `null`
  （例：`input=1368, cache_read=388992, output=719`；透传行 tokens 全 `null`）。
- **隐私扫描**：整文件不含 relay token、`Bearer`、`Authorization`，也不含任何 prompt 正文
  （`只回复两个字` / `秋天的仓库` / `分布式限流器` 均未出现）；只出现模型名与账号名（设计如此）。
- `outcome` 分布：`ok=50, passthrough=2`；`sticky` 分布：`hit=41, miss=11`。

### 4.2 `scripts/stats.py` 实跑（原样输出）

```
requests.jsonl · ~/.dsh/opencode-pool/requests.jsonl
总请求: 51  成功率: 96.1% (49/51)
outcome: ok=49 passthrough=2
延迟(ms): p50=6255.2 p95=15146.6
缓存命中率: 99.7% (cache_read=17791232, input=59154)
粘性命中率: 78.4% (hit=40 miss=11)
限流请求: 0  换号次数: 0
按账号:
  go02       请求 51    失败 2     平均TTFB 3485.1ms  中位 3623.3ms
```

文档记载的两个边界也都实测过：追加 2 行坏数据后输出 `跳过坏行: 2` 且不崩；
`--since 5x` 报 `--since 只支持 Ns/Nm/Nh/Nd，收到 '5x'`；`--since 12h` 正常汇总。

## 5. 取消流式请求的实测行为（**唯一不达标项**）

按任务要求取消一个流式请求（共试了 3 种取消时机：0B 前取消 / 3s / 5–6s 中途取消），实测：

- `relay.log`：**只有 1 行 `[req]`（或 1 行 + 1 条其它客户端行），没有任何 `[http] 客户端断开` 摘要**，
  也**没有 Traceback**；等待 20s 后同样没有迟到摘要。
- `requests.jsonl`：有对应行，但 `outcome=ok`、`status=200`、`bytes` 为「已计数」而非「已送达」
  （例：客户端 0B 收到、日志 bytes=207；客户端 70386B、日志 bytes=70932）。

**机制**：t5 把断连异常在 `_relay` / `_raw` / `_serve_panel` 的每个写回点就地 `except (BrokenPipeError,
ConnectionResetError)` 吞掉了，异常不会再冒到 `RelayServer.handle_error`，所以「取消流式请求」这条路径
**不会**打印摘要。日志里那 68 条摘要来自另一条路径：keep-alive 连接被客户端重置后，下一次
`handle_one_request` 读请求行失败 → 冒泡到 `handle_error`（摘要里的 path 正是该连接上**上一条**请求）。

**后果**：① 验收要求的「取消后有一行 `[http]` 摘要」不成立；② 取消的请求在 `stats.py` 里被算成
`outcome=ok`（本轮 2 次取消都进了 `ok=49`），成功率因此偏高、与「完整送达」不可区分；`bytes` 也偏大。

**建议**（二选一，由 captain 定）：
- 在 `_relay` 捕获断连处补一行同样的摘要 `[http] 客户端断开 <method> <path> (<异常>)`，并把
  `outcome` 置为已有的 `client_error`（`reqlog.OUTCOMES` 里就有这一档）+ `error_reason=client_abort`；
- 或放宽验收口径为「取消后日志无 Traceback」并把 `client_abort` 语义写进 `docs/analytics.md`
  （不改代码，但要知道成功率会含取消请求）。

## 6. 文档与实现抽查（4 组）

| 项 | 结果 | 证据 |
| --- | --- | --- |
| `request_log_path` / `request_log_max_bytes` / `request_log_keep` | **一致** | `configuration.md` 第 28–30 行示例与第 70–72 行表格给出三个字段与默认值；与 `config.py` 默认值逐字相同（`~/.dsh/opencode-pool/requests.jsonl`、`20971520`、`3`），并有「结构化请求日志」整节说明 `""`=关闭、相对路径按配置目录解析、轮转 `.1 .2 …` 保留 N 份 |
| `stats.py` 用法 | **一致** | `docs/analytics.md` 记录 `--file` / `--since Ns·Nm·Nh·Nd` / 默认路径 / 坏行跳过计数 / 每个数怎么读；实测输出字段与之一一对应（总请求、成功率、outcome、p50/p95、缓存命中率、粘性命中率、限流/换号、按账号） |
| `/metrics` 九族 | **一致** | 实际抓取 9 个 `# TYPE`；`docs/troubleshooting.md` 的指标表逐行列全 9 个名字与语义 |
| `/healthz` reason 码表 | **一致** | 代码 8 个 reason（`no-accounts / all-disabled / all-cooling / all-exhausted / all-below-reserve / usage-stale / usage-not-sampled / missing-credentials`）+ `ready`；`troubleshooting.md` 全部列出；本轮实测到 `ready` |

**快照说明**：本验收开始（19:03）时 `docs/` 里还没有 `analytics.md`，`configuration.md` 也没有
`request_log_*` 三行；它们是在 19:07:30/19:07:54 补齐的（应是 t9 的文档交办在我验收期间落地）。
上表是对 **19:07 快照** 的核对结果，全部一致。若之后文档再动，需要重新核对一次。

## 7. 未解决 / 建议

1. **（本任务唯一不达标）取消流式请求无 `[http]` 摘要、并在统计里算成功** —— 详见第 5 节，附两条修法。
2. `start.sh` 启动瞬间的提示语把「首轮采样未完成」说成「没有可调度账号」，容易误导（建议区分两种原因）。
3. 历史遗留（前几轮已记录、本轮未动）：L1 流式记账帧识别用 `"usage"` 子串启发式；L3 已由 t8 修（加锁 + 去缓存）；
   其余无新增。
4. 账号侧现状未变：go01（weekly 100%）、go25（monthly 99%）不可调度，池子实际只有 go02；
   go02 一冷却 `/healthz` 即转 503（配额现状，非缺陷）。

## 8. 结论

除第 5 节一项外，其余验收项全部通过：测试 10/10 + node 5/5 + `verify.sh 20` STABLE；
重启后三端点正常（9 族指标）；`requests.jsonl` 21 字段、可解析、无敏感内容，`stats.py` 能汇总出
非零请求/缓存命中率/粘性命中率；`req=<id>` 可与 `relay.log` 对账；文档四组一致（19:07 快照）。

**总判定：不通过（1 项）** —— 取消流式请求的场景与验收期望不符（0 行摘要 + 统计算 ok）。
该项属诊断/统计口径问题，不影响中转可用性；是否阻断发布请 captain 定夺，建议按第 5 节二选一处理。

## 9. 复跑命令

```bash
cd <project-dir>
for i in $(seq 1 10); do python3 -m unittest discover -s tests 2>&1 | tail -1; done
node --test tests/plugin.test.mjs
./scripts/verify.sh 20
./scripts/start.sh stop && ./scripts/start.sh start && sleep 3
curl -s http://127.0.0.1:8790/healthz; curl -s http://127.0.0.1:8790/metrics | grep -c '^# TYPE'
TOKEN=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.dsh/opencode-pool/pool.config.json')))['relay_token'])")
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8790/v1/usage | python3 -m json.tool | head -30
python3 scripts/stats.py --since 24h
```
