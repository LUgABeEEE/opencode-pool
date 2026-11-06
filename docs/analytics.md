# 分析：`requests.jsonl` 与 `stats.py`

结构化请求日志（`requests.jsonl`）的字段清单与三个关键语义在
[configuration.md](configuration.md) 的「结构化请求日志」一节；本文讲**怎么用它做分析**。

## `stats.py`：零依赖的日报式汇总

只用标准库（不需要 jq / pandas），直接吃 `requests.jsonl`：

```bash
python3 scripts/stats.py               # 统计整个文件
python3 scripts/stats.py --since 24h   # 只看最近 24 小时
python3 scripts/stats.py --file /path/to/requests.jsonl
```

`--since` 支持 `Ns` / `Nm` / `Nh` / `Nd`（例如 `90m`、`24h`、`7d`），缺省统计整个文件；
`--file` 缺省是 `~/.dsh/opencode-pool/requests.jsonl`（轮转出去的 `.1` `.2` 不会自动合并）。
坏行（半截 JSON / 手工改坏）会被跳过并计数，不会让脚本崩。

实测输出示例（2026-11-06，本机；首行是日志路径，「按账号」每个账号一行）：

```
requests.jsonl · ~/.dsh/opencode-pool/requests.jsonl
总请求: 14  成功率: 92.3% (12/13 完成请求)
client_error: 1 (7.1% of all)
outcome: ok=12 passthrough=1 client_error=1
延迟(ms): p50=8445.1 p95=20670.2
缓存命中率: 99.8% (cache_read=5975424, input=12612)
粘性命中率: 76.9% (hit=10 miss=3)
限流请求: 0  换号次数: 0
按账号:
  go02       请求 13    失败 1     平均TTFB 3046.1ms  中位 3440.3ms
```

## 每个数怎么读

| 输出 | 怎么读 |
| --- | --- |
| 总请求 / 成功率 | **成功率 = 完成请求里 `status<400` 且非 `client_error` 的比例；分母是「完成请求」= 总请求 − `client_error`**。`passthrough`（客户端 4xx 原样回）计入非成功侧。被取消的请求单列在 `client_error`，不混进分母，所以成功率不会因取消而虚高或虚低 |
| `client_error` | 客户端在响应写完前断开/取消（`error_reason=client_abort`）：请求行 `outcome=client_error`、`status=499`、`bytes` 记**实际写出字节数**（0 表示客户端一个字节都没拿到）；`relay.log` 对应一行 `[http] 客户端断开 <method> <path> (<异常>)`，`<path>` 是**本次**请求。它不算成功，也不算上游失败——单独一列，便于和浏览器/Agent 的取消行为对账 |
| `outcome` | `ok` 上游成功 · `passthrough` 4xx 透传 · `throttled` 限流 · `failover` 换号后仍失败 · `no_account` 503 没号 · `client_error` 客户端取消/断开（`client_abort`） |
| 延迟 `p50` / `p95` | 端到端总耗时分位。觉得慢先分侧：**`queue_ms` 高 = 池子在排队（本地瓶颈：并发闸 / 冷却等待 / 重试退避），不是上游慢；`ttfb_ms` 高才是上游出字慢** |
| 缓存命中率 | **高 = 粘性在生效、前缀被复用**（口径 `cache_read / (cache_read + input)`，详见 configuration.md 的口径说明）。注意它**不是成本节省率** |
| 粘性命中率 | **低 = 账号频繁不可用（反复重选）或 `sticky_ttl_seconds` 太短**；配合 `pool_sticky_misses_total` 与冷却日志定位 |
| 限流请求 / 换号次数 | 分别对应 `error_reason` 限流类与 `failovers > 0` 的行；持续增长说明上游在压速率，看 `max_inflight` / `min_interval_seconds` 是否要收紧 |
| 按账号 | 平均 / 中位 TTFB 是各账号的出字速度；账号之间对比，用来挑"快而稳"的号、或发现某把 key 被降速。**失败数包含被取消的请求**（`client_error` 也算一条失败行），与 JSONL 逐行对账 |

## 和 `/metrics` 的关系

- `/metrics` = **运行态快照**（gauge/counter，给 Prometheus 抓）；
- `requests.jsonl` = **逐请求事件流**（给离线分析、回溯单次请求）。
- 两者与 `/v1/usage` 同源于一份账号状态；口径对不上先怀疑统计脚本读的不是同一份文件。
- 取消口径是**跨三处一致**的：`relay.log` 一行摘要（连接级事件，见「已知限制」F2）→
  `requests.jsonl` 的 `outcome=client_error` / `error_reason=client_abort` /
  `bytes=<实际写出量>` → `stats.py` 的 `client_error` 行与成功率分母。

## 已知限制（0.2.0）

两条都来自 t14 复审，**计划 0.2.1 修复**；修复前请按下面的"以谁为准"读日志。

### F1 · 非流式取消时人类日志行会显示 200

**现象**：非流式请求被客户端取消时，`relay.log` 的 `[req] … 200 … bytes=123`
是在写响应**之前**打印的，断开发生在打印之后、catch 里改不回已输出的行，
所以人类日志仍显示 200。

**以谁为准**：**`requests.jsonl` 与 `stats.py` 是权威口径**
（`status=499` / `outcome=client_error` / `bytes` 为实际写出量）；
`relay.log` 的人类行是尽力而为。**流式分支没有这个问题**
（取消会显示 499，且 `bytes` 只报实际写出量）。

**计划修复**：把非流式 `[req]` 打印移到写响应之后（0.2.1）。

### F2 · `[http] 客户端断开` 摘要行没有 `req_id`

**现象**：这条摘要是**连接级**事件，不是业务请求级——同一连接上
「响应已写完、客户端随后重置 keep-alive」也会产生一条。因为没有 `req=<id>`，
在共享日志里看起来像"同一请求有两条摘要"（复审时就误读过一次）。

**怎么读**：**摘要是连接级的、数量可能多于请求数**；要精确归因，
看 `requests.jsonl` 里 `req_id` 对应的那行（一个请求恰好一行）。

**计划修复**：给摘要行加 `req=<id>`（handler 上已有 `_request_record`），
并在 troubleshooting 写明"连接级事件也会产生摘要"（0.2.1）。

## 隐私

事件流不写 prompt / response 正文与 Authorization；`req_id` 是随机值、不派生自请求
内容。把它当排障数据分享没问题，但整份文件仍可能暴露**账号名与用量**，
外发前自行脱敏。
