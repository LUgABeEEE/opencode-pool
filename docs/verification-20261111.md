# 过载排队与容量修复（0.2.1 · 2026-11-11）

- 现象起点：用户反馈「号池现在还是有点问题」；`requests.jsonl` 里 11 点档 453 请求 / 102 非 200，
  15 点档 40 请求 / 38 非 200 —— 近乎全灭，`queue_ms` 稳定卡在 60s / 90s。
- 环境：`/data/agent/workspace/opencode-pool`（git 仓库，工作区干净时 HEAD=65413fe）·
  运行态 `~/.dsh/opencode-pool/`（配置 / `pool_state.json` / `relay.log` / `requests.jsonl`）·
  监听 `127.0.0.1:8790`，DSH 默认 provider 就是它（所有 agent 流量都过这里）。

---

## 1. 证据：失败长什么样

`requests.jsonl` 里 503 行的形状（15:00 档）：

```
{"ts":"2026-11-11T15:06:20.553+08:00","account":null,"status":503,
 "outcome":"failover","queue_ms":60008.2,"total_ms":60008.2,
 "failovers":2,"error_reason":"concurrency-timeout"}
```

- `queue_ms ≈ 60000` 或 `90000`：60000 = 两个号各等 30s；90000 = 三个号各等 30s。
  **这正是问题的手印**：等待是"按号累加"的，不是按请求。
- 同期 `relay.log`：`并发排队超时 → 本次请求换号（会话钉子保留）` 共 1253 条，
  客户端断开 1546 条 —— 客户端的耐心（约 60s）比中转的等待（最多 90s）更短。
- 容量侧：池子里 4 个号，`go25` 月额度 100% 被面板暂停 → 3 个可调度；
  `max_inflight=1` → 同时只能有 3 条在飞，而实测客户端峰值并发 **14**。

## 2. 根因（4 条，全部可复现）

1. **并发位等待用了签名默认值**：`_run_proxy` 调 `pool.acquire(account)` 不传 timeout，
   走的是 `def acquire(..., timeout=30.0)`。一个号等 30s，等不到就"换号"，下一个号再等 30s。
   `queue_wait_seconds` 只管"全池冷却"那条路径，管不到容量等待 —— 所以设成 3s 也没用
   （基线压测里 20 并发照样等到 30.9s）。
2. **容量等待被当成"这个号坏了"**：等不到并发位 → 解钉 + `failovers += 1` + 把号加进 `tried`。
   于是位子后来空出来了，本次请求也不会回头用，只能一路换到 503。
3. **429 退避占着并发位睡觉**：`_attempt` 里 `time.sleep(2/4/8s)` 发生在调用方释放并发位之前 ——
   一个号唯一的坑位被"睡觉"占着，别人进不来。
4. **客户端走了不知道**：排队 / 等位期间完全不看连接状态，到点还替死连接打一次上游（白烧额度）。

## 3. 改动

见 `CHANGELOG.md` 0.2.1 段。摘要：全局排队期限（一个 `queue_wait_seconds`）、
容量等待不算换号不解钉不排除、退避睡眠移出并发位、`MSG_PEEK` 探测客户端是否已断开、
网络层抖动先同号重试一次、`冷却 0 分钟` 文案、`stats.py` 测试时间戳腐烂。

## 4. 验证

### 4.1 自动化

| 命令 | 结果 |
| --- | --- |
| `python3 -m unittest discover -s tests` | `Ran 118 tests in 20.0s` → **OK**（新增 2 条回归） |
| `node --test tests/plugin.test.mjs` | `pass 5 / fail 0` → **OK** |
| `python3 scripts/load-smoke.py` | `PASS` |

> 修复前跑全量是 **1 红**（`StatsScriptTest` 写死了 2026-11-06 的时间戳，过了那天必红）。

### 4.2 端到端压测（`scripts/load-smoke.py`，假上游每请求 1.5s，2 个号，每号 1 并发位）

| 场景 | 0.2.0（修改前） | 0.2.1（修改后） |
| --- | --- | --- |
| A 8 并发 · 预算 10s | 8/8 成功，最慢 **12.35s** | 8/8 成功，最慢 **8.51s** |
| B 20 并发 · 预算 3s | 20/20 成功但最慢 **30.89s**（预算被无视） | 4 成功 + 16 个按预算 **3.0s** 收口，最慢 4.5s |
| C 客户端排队中途断开 | 仍为死连接打了 **1 次**上游 | **0 次** |

### 4.3 生产实测（0.2.1 上线后，同一负载：6 并发 / 小 prompt）

| `max_inflight` | 6 并发总耗时 | 单条最慢 | 503 | 上游 429 |
| --- | --- | --- | --- | --- |
| 1（原值） | **43.0s** | 42.99s | 0 | 0 |
| 2 | **14.0s** | 13.99s | 0 | 0 |
| 3 | 15.7s（9 并发） | 15.74s | 0 | 0 |
| 4 | 38.2s（12 并发） | 38.19s | 0 | 0 |

`max_inflight=4` 那轮的单条排队只有 1～10s，剩下 37s 全是上游自身耗时 —— 说明瓶颈回到上游，
**并发位加到 3 已经够**，再往上收益不明显。故生产配置定为 **`max_inflight: 3`**
（3 个可调度号 × 3 = 9 个并发位，12 并发实测未触发任何 429）。

## 5. 线上状态与回滚

- 重启：`/data/agent/workspace/SER/restart-pool-0.2.1.sh`（脱离会话运行、健康检查 20s、
  失败自动回滚旧代码并重启）。实际：15:33:46 发 SIGTERM，15:33:48 新进程就绪
  （pid 1433351 → 1546846），`/healthz` 200、`ready=3`。
- 配置：`~/.dsh/opencode-pool/pool.config.json` 增加 `"max_inflight": 3`
  （备份 `pool.config.json.bak-*-before-maxinflight-2`），面板热重载生效、无需重启。
- 回滚代码：`tar -xzf /data/agent/workspace/SER/_pool_backup-20261111-153252.tgz -C /data/agent/workspace/opencode-pool`
  然后重启中转。回滚配置：把 `max_inflight` 改回 1（或删掉该键，默认即 1）。
- 上线后 10 分钟观测：`requests.jsonl` 中 13 条为压测流量，全部 200，
  `failovers` 全部为 0（旧代码里容量等待会记 failover）。

## 6. 仍未做 / 待观察

- 未提交 git（用户未要求 commit），工作区为修改态。
- `go25` 月额度 100%（重置时间见面板），恢复后才算真正 4 个号；若并发继续升高，
  优先加号而不是继续抬 `max_inflight`。
- `upstream_timeout` 仍是 900s：上游偶尔 30s+ 才出首字，客户端侧超时策略不在本仓库内。
