# 排障判据

按"你看到的现象"查。所有错误文本都以代码里的真实措辞为准（0.2.0）。

## 速查表

| 现象 | 大概率原因 | 跳到 |
| --- | --- | --- |
| DSH 面板 502 | 中转进程没起来（或被插件拉起失败） | [面板 502](#面板-502dsh-插件入口) |
| 直连面板 403 `panel is loopback-only` | 从非 loopback 直连了中转端口 | [面板 403](#面板-403panel-is-loopback-only) |
| 调接口 503 `NoAvailableAccount` | 全部账号冷却 / 额度耗尽 / 低于水位线 | [号池 503](#号池-503noavailableaccount) |
| 调接口 401 `bad relay token` | 客户端没带 / 带错 `relay_token` | [鉴权 401](#鉴权-401) |
| DSH 整页 boot 卡在 `Failed to load plugins` | 插件客户端模块 id 写错 | [DSH boot](#dsh-boot-卡住import-failed) |
| CLI 报 `unrecognized arguments` | 旧版 `--config` 位置问题（0.1.1 已修） | [CLI --config](#cli---config-位置) |
| 不知道日志在说什么 | `[req]` / `[pool]` 行格式 | [读日志](#req-与-pool-日志怎么读) |
| Prometheus 抓不到 / 看不懂指标 | `/metrics` 指标含义 | [指标含义](#metrics-指标含义) |
| `/healthz` 回 503 | 就绪语义：无可用账号或采样过期 | [healthz](#healthz-200--503-语义) |
| 想统计请求量 / 缓存命中 / 延迟 | `requests.jsonl` + `stats.py` | [analytics.md](analytics.md) |

---

## 面板 502（DSH 插件入口）

面板经 dsh-web 代理（`/api/opencode-pool/panel`）时，502 = **中转没在后面跑**：

1. 看中转进程：`python3 -m opencode_pool status`（读不到 = 没起）；
2. 手动起一次看报错：
   ```bash
   python3 -m opencode_pool serve --config ~/.dsh/opencode-pool/pool.config.json
   ```
   起不来的常见原因：配置校验不过（逐条打印问题）、端口被占、缺 `requests`；
3. 插件托管模式下看 dsh-web 日志里的提示
   （`确认 python3 -m opencode_pool serve 能起来，或把 autostart 打开`）：
   - `autostart` 关了就自己起中转；
   - `projectDir` 指错了（插件要能看见 `opencode_pool/__main__.py`）；
4. 历史坑：0.1.0 的 CLI 只认子命令**前**的 `--config`，插件用
   `serve --config X` 会 `unrecognized arguments` 秒退 → 面板 502。
   0.1.1 已修，两个位置都接受。升级后仍 502 就不是这个问题。

## 面板 403（`panel is loopback-only`）

直连 `http://<机器IP>:8790/` 时面板拒绝 —— 这是**故意的**（见 SECURITY.md）：
面板只对 127.0.0.1 提供。想从局域网看面板，走 DSH 插件入口
（`/api/opencode-pool/panel`，鉴权沿用 dsh-web 登录），
或 SSH 端口转发：`ssh -L 8790:127.0.0.1:8790 <主机>`。

## 号池 503（NoAvailableAccount）

响应体：`{"type":"error","error":{"type":"NoAvailableAccount","message":"号池里没有可用账号（…）"}}`，
括号里是最后一次失败的细节（如 `go02: quota:weekly`、`go01: 缺凭据`、
`排队超时（全池仍在冷却）`）。

判据顺序：

1. `python3 -m opencode_pool status` 看每个账号的「余量 / 绑定窗口 / 备注」；
   - 备注 = 冷却原因：`quota:weekly`（周额度打满，冷到窗口重置）、`auth:401`（key 失效）、
     `upstream:5xx`、`net:<异常名>`（网络层失败）、`missing-key`（槽位里没值）、
     `throttled-persistent`（限流持续，冷 15s）；
2. 面板看「余量保留中 · 不参与调度」——余量低于 `min_headroom`（默认 2.0）的号
   会被主动跳过，这是特性不是故障；想让它接单就调低水位线；
3. 看到 503 前先留意**排队**：全池瞬时限流时请求会先等 `queue_wait_seconds`
   （默认 60 秒，日志见 `全池冷却中，排队等待`）才回 503 —— 等待中不算失败，
   嫌等太久就调小这个值；
3. 真全死了：面板「清冷却」（`POST /v1/pool/clear-cooldowns`）手动放出来试试，
   或换 key（`POST /v1/pool/account/key`）；
4. 只有一个号还打了满额度：等窗口重置（面板的窗口卡有倒计时），或加号。

## 鉴权 401

- `bad relay token`：客户端的 `Authorization: Bearer <...>` 要用配置里的
  `relay_token`（`init` 生成的那串）。DSH 插件路径下浏览器不需要 —— 宿主侧注入；
- `unauthorized`：管理 API（`/v1/usage`、`/v1/pool/*`）同样要 token；
- `relay_token` 为空 = 不鉴权；`check` 一下是不是配置读错了文件。

## DSH boot 卡住（import failed）

整页停在 `Failed to load plugins`，且每次点名的插件可能不同 —— DSH 的 web boot
是"全有全无"，一个客户端条目没起来就全停。对着报错形态判：

| 报错 | 含义 | 翻哪 |
| --- | --- | --- |
| `import failed: client-modules: could not load "X" without registering` / `already executed without registering` | 模块 id 不对，或文件没包 `__ModuleLoader__.load` | `dsh-plugin/lib/client.js` 第一行的 `id`：**必须等于 npm 包名**（`dsh-plugin-opencode-pool`），不是 cordis 条目 id |
| `X: failed`（没有额外说明） | 插件 `apply()` 期间抛错 | 该插件的宿主侧入口 `lib/index.js` |

防回归断言在 `tests/plugin.test.mjs`（"模块 id 必须是 npm 包名"），
改插件前先跑 `node --test tests/plugin.test.mjs`。

## CLI --config 位置

两种写法都对（0.1.1 起）：

```bash
python3 -m opencode_pool --config /path/pool.config.json serve   # 顶层
python3 -m opencode_pool serve --config /path/pool.config.json   # 子命令后（插件/脚本用这种）
```

不给 `--config` 时取环境变量 `OPENCODE_POOL_CONFIG`，再退到默认
`~/.dsh/opencode-pool/pool.config.json`。
报 `unrecognized arguments` = 在用 0.1.0 的旧代码。

## `[req]` 与 `[pool]` 日志怎么读

日志打到 stdout（systemd 走 `journalctl -u opencode-pool`，`start.sh` 走
`~/.dsh/opencode-pool/relay.log`）。access log 是静音的，只有下面几类行：

```
[req] go01 deepseek-v4.1-flash 200 total=2.31s bytes=1834              # 非流式成功
[req] go02 deepseek-v4.1-flash 200 stream ttfb=0.84s total=6.12s bytes=9213   # 流式成功
[req] go02 deepseek-v4.1-flash 429 quota:weekly → 换号                 # 失败换号（reason 来自适配器）
[req] go01 deepseek-v4.1-flash 429 瞬时限流 → 2.0s 后同号重试 (1/3)    # 同号指数退避重试（2→4→8s）
[req] go01 deepseek-v4.1-flash 429 限流持续 → 冷却 15s 并换号          # 重试预算耗尽，改冷却换号
[req] deepseek-v4.1-flash 全池冷却中，排队等待                         # 所有号都在冷，请求在等
[req] go01 gpt-x 400 透传                                              # 客户端错误原样回，不换号
[sticky] go02 不可用 → 解除本对话钉子                                  # 粘性解钉，下一轮重新选号
[pool] go02 冷却 2.1 天 原因=quota:weekly                             # 账号进冷却
[pool] listening http://127.0.0.1:8790  providers=[...]  accounts=[...]  # 启动横幅
[pool] state save failed: ...                                          # 状态持久化失败（磁盘/权限）
```

读法：

- `[req]` = **账号 模型 状态 处置**。`→ 换号` 前面的词是失败原因
  （`quota:<窗口>` = 该档打满；`auth:401` = key 失效；
  `net:<异常名>` = 网络层异常）；
- 瞬时限流（429 但额度还有的那种）走**同号指数退避重试**（2s → 4s → 8s），
  重试预算耗尽才 `限流持续 → 冷却 15s 并换号`；`全池冷却中，排队等待` 说明
  请求在等冷却结束，不是失败；
- `[sticky] … 解除本对话钉子` = 粘性路由在账号不可用时主动解钉，下一轮重新选号；
- `ttfb` 是首字节耗时、`total` 是总耗时、`bytes` 是响应体大小；
- `透传` = 上游 4xx 但换号没用（403 地区限制 / 400 参数错），原样回给客户端；
- `[pool] 冷却 X 原因=…` 里的时长是冷却多久（`5 小时` / `2.1 天` 这种可读格式）；
- 日志**不含**密钥与 prompt 正文（这是硬约束，见 SECURITY.md）。
- **已知限制（0.2.0，计划 0.2.1 修）**：① 非流式请求被客户端取消时，人类行仍显示
  `200`（打印在写响应之前，无法回改）——**以 `requests.jsonl` / `stats.py` 为准**
  （`status=499` / `outcome=client_error`）；流式分支没有这个问题。
  ② `[http] 客户端断开 …` 摘要是**连接级**事件（响应写完后 keep-alive 被重置
  也会来一条），数量可能多于请求数、且没有 `req=<id>`；精确归因看
  `requests.jsonl` 的 `req_id`。详见 [analytics.md](analytics.md) 的「已知限制」。

## `/metrics` 指标含义

0.2.0 起提供 Prometheus 文本格式（`GET /metrics`）：

| 指标 | 类型 | 含义 |
| --- | --- | --- |
| `pool_account_headroom{account,provider}` | gauge | 账号余量 = `100 − max(各窗口 percent)`（即各窗口剩余量的最小值）；`-1` = 冷却中 / 打满 / 暂停；不提供余量观测的适配器不输出该序列 |
| `pool_account_window_percent{account,window}` | gauge | 单档额度已用百分比（0–100），`window` 是 `rolling` / `weekly` / `monthly` 这类声明的 key |
| `pool_requests_total{account,model,status}` | counter | 经手的请求数（含换号尝试），`status` 是上游返回码或本地处置码 |
| `pool_failovers_total{account,reason}` | counter | 换号次数，`reason` 与日志同源（`quota:weekly` / `auth:401` / `upstream:503` / `throttled-persistent` / `net:*` …） |
| `pool_cooling_seconds{account}` | gauge | 该账号还要冷却多少秒（0 = 没在冷却） |
| `pool_sticky_hits_total` | counter | 会话粘性命中（对话被钉回原账号）次数 |
| `pool_sticky_misses_total` | counter | 会话粘性未命中（新对话 / 钉子过期 / 账号不可用重选）次数 |
| `pool_cache_read_tokens_total{account}` | counter | 上游 prompt cache 命中的输入 token 累计（`usage.opencode.tokens.cache_read`）——"粘性有没有保住缓存"的硬证据 |
| `pool_input_tokens_total{account}` | counter | **未命中缓存**的输入 token 累计；命中率 = `cache_read / (cache_read + input)` |

- 标签值按 Prometheus 文本格式转义（`"` `\` `\n`）；
- `/metrics` 与 `/v1/usage` 的账号状态同源，两处对不上就是 bug；
- **鉴权**：本机（127.0.0.1）直接 curl 免 token；从其他机器抓取需带
  `Authorization: Bearer <relay_token>`；
- 粘性命中率低（`pool_sticky_misses_total` 比 `pool_sticky_hits_total` 涨得快）
  通常意味着账号频繁不可用（反复重选）或 `sticky_ttl_seconds` 太短；
  对应的 `/v1/usage` `sticky` 计数怎么读见 [configuration.md](configuration.md)；
- **缓存复用怎么读**：`cache_read` 占比高（对照 `/v1/usage` 账号条目的
  `cache_hit_ratio` = `cache_read / (cache_read + input)`）= 粘性生效、前缀被复用。
  **流式与非流式请求都计入**（流式取 SSE 最后一帧带 `usage` 的 `data:` 行）。
  记住它**不是成本节省率**（只是前缀复用程度）；何时接近 1、口径细节见
  [configuration.md](configuration.md)。
  占比长期为 0 先看两件事：
  ① prompt 太短 —— 实测 43 token 的 prompt 不会命中缓存（上游行为，别拿它当健康判据）；
  ② 账号被频繁切换 —— 配合 `pool_sticky_misses_total` 与冷却日志看。

## `/healthz` 200 / 503 语义

0.2.0 起 `/healthz` 是**就绪探针**（readiness），不只是存活：

- **200** = 至少一个账号可调度 **且** 额度采样新鲜
  （采样年龄 < `2 × usage_refresh_seconds`）；
- **503** = 没有可调度账号，或采样过期 / 启动后尚未完成首轮采样。
  body 是 JSON（**不含密钥**），`reason` 一眼定位原因：
  `no-accounts`（没配账号）/ `all-disabled`（全部暂停）/
  `missing-credentials`（账号缺凭据，采不了额度）/ `all-cooling`（全部冷却）/
  `all-exhausted`（全部打满）/ `all-below-reserve`（全部低于水位线）/
  `usage-stale`（采样过期）/ `usage-not-sampled`（首轮采样未完成）。
  **选取规则**（别记反）：多档同时阻塞时，报**阻塞账号数最多**的那档；
  数量并列时按 `missing-credentials` → `all-cooling` → `all-exhausted` →
  `all-below-reserve` → `usage-stale` 的先后取（即 `missing-credentials`
  排在 `usage-stale` 之前）；`usage-not-sampled` 是 `usage-stale` 的细分——
  从未完成过采样时用它。另有 `message`（人类可读，**文案以实际返回为准**）、
  `accounts`（按状态分档的计数）、`fresh_within_seconds` /
  `oldest_sample_age_seconds`（采样新鲜度）等字段；
- 常见误报场景：刚启动的几秒内首轮 `/v1/usage` 还没回来 → 503，等几秒就好；
  Docker HEALTHCHECK 的 `start-period` 已留 30 秒余量；
- **单点运维提示**：可用账号数为 1 时，任何一次限流冷却都会让 `/healthz`
  短暂 503（就绪语义的正确行为，不是故障）。想避免，至少保留**两个**可调度
  账号，或用 `min_headroom` 别把号压到水位线下；
- **判存活看端口 / 进程，判可用性看 `/healthz`**：`scripts/start.sh` 用
  "端口 8790 是否在监听"判存活（pid 文件会被插件拉起的实例覆盖；
  `/healthz` 是就绪语义，拿它判存活会把"起来了但没号"误判成"没起来"）。
  `/healthz` 未 200 只说明"起来了但没有可调度账号"，看
  `./scripts/start.sh status` 找原因。

## 其他常见问题

- **面板加号报"写配置失败"**：配置文件权限 / 磁盘满；校验不过时会拒绝落地并给原因；
  手改配置改坏了 → 看同目录 `*.bak-<时间戳>-panel` 备份；
- **`check` 报"未知适配器"**：`adapters/ext/` 里的文件加载崩了 ——
  `/v1/pool/schema` 的 `adapter_load_errors` 里有具体异常；
- **状态丢了**（重启后冷却/累计归零）：`state_file` 是不是被置空 / 路径写到了别处；
  相对路径按配置文件所在目录解析；
- **密钥写不进**（Docker）：挂载目录属主与容器 uid 不一致，见
  [../deploy/README.md](../deploy/README.md) 的 Docker 节。
