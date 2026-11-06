# 配置参考（逐字段）

配置是一份 JSON，默认在 `~/.dsh/opencode-pool/pool.config.json`（`init` 生成，`0600`）。
加载顺序：CLI `--config`（顶层或子命令后都行）> 环境变量
`OPENCODE_POOL_CONFIG` > 默认路径。改完先跑 `python3 -m opencode_pool check`。

配置结构是 **v2**（`providers` + `accounts` 分离）；v1（账号平铺在顶层的老写法）
会被自动迁移并提示，不需要手改。

## 一份完整示例

```json
{
  "version": 2,
  "listen": "127.0.0.1:8790",
  "relay_token": "pool-xxxxxxxx（init 生成的随机串）",
  "credentials_yaml": "~/.dsh/.credentials.yaml",
  "secrets_file": "~/.dsh/opencode-pool/secrets.json",
  "state_file": "pool_state.json",
  "pid_file": "relay.pid",
  "usage_refresh_seconds": 90,
  "upstream_timeout": 900,
  "min_headroom": 2.0,
  "max_inflight": 1,
  "min_interval_seconds": 1.2,
  "queue_wait_seconds": 60.0,
  "sticky_ttl_seconds": 43200,
  "request_log_path": "~/.dsh/opencode-pool/requests.jsonl",
  "request_log_max_bytes": 20971520,
  "request_log_keep": 3,
  "providers": {
    "opencode-go": {
      "adapter": "opencode-go",
      "base_url": "https://opencode.ai/inference/go/v1"
    },
    "local-vllm": {
      "adapter": "openai-compatible",
      "base_url": "http://127.0.0.1:8794/v1",
      "models": ["qwen3-32b"],
      "headers": {"X-Tenant": "lab"}
    }
  },
  "accounts": [
    {"name": "go01", "provider": "opencode-go", "key_env": "OPENCODE_KEY_01",
     "session_prefix": "dsh-pool-01"},
    {"name": "go02", "provider": "opencode-go", "key_env": "OPENCODE_KEY_02"},
    {"name": "vllm-01", "provider": "local-vllm", "key_env": "VLLM_KEY"}
  ]
}
```

## 顶层字段

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `version` | int | `2` | 配置结构版本；写回时固定为 2 |
| `listen` | string | `"127.0.0.1:8790"` | 监听地址 `host:port`。默认只听 loopback；**容器里要写 `0.0.0.0:8790`** |
| `relay_token` | string | `""` | 客户端调 `/v1/*` 与管理 API 的 Bearer token。`init` 会生成 `pool-` + 24 字节随机串；**留空 = 关闭鉴权**，生产请写上 |
| `credentials_yaml` | string | `"~/.dsh/.credentials.yaml"` | DSH 凭据库（**只读回退**）：环境变量、密钥仓都没有时，从它的 `refs:` 段取同名 key |
| `secrets_file` | string | `"~/.dsh/opencode-pool/secrets.json"` | 面板可写的密钥仓（目录 `0700` / 文件 `0600`）。只存 key 的值，配置文件本身不含密钥 |
| `state_file` | string | `"pool_state.json"` | 账号状态持久化（额度采样、累计、冷却）。相对路径按**配置文件所在目录**解析；置 `""` 关闭持久化 |
| `pid_file` | string | `"relay.pid"` | `serve` 启动时写自己的 pid（供 `start.sh` 判断在不在跑）。同样相对配置目录解析 |
| `usage_refresh_seconds` | int | `90` | 额度采样周期（秒）。下限 15；它同时决定"采样新鲜"的判据（`/healthz` 就绪语义里按 `2 × 该值` 判过期） |
| `upstream_timeout` | float | `900` | 上游请求超时（秒）。默认给得很宽是因为流式长回答会跑很久 |
| `min_headroom` | float | `2.0` | 余量水位线：`headroom < 该值` 的账号不参与调度（面板标「余量保留中 · 不参与调度」）。贴着死线的号上游常"先接流再不出字"，比 503 更难排；想榨干最后一滴设 `0` |
| `max_inflight` | int | `1` | 每个账号同时最多几个在飞请求。上游对并发敏感（实测 5+ 并发就回裸 429），排队比撞限流体面。`0` = 不限制 |
| `min_interval_seconds` | float | `1.2` | 同一账号两次请求的最小间隔（秒）。上游按速率限流（实测 8 连击回裸 429、间隔 1.5s 串行全过） |
| `queue_wait_seconds` | float | `60.0` | 全池都在瞬时限流冷却时，最多陪客户端等多久（秒）再回 503。只有一个号时它决定"排队"还是"直接失败" |
| `sticky_ttl_seconds` | int | `43200`（12 小时） | **0.2.0 新增**：会话粘性时长（秒）。同一对话（指纹 = model + system + 首条 user，公式带 `SEED_VERSION`：升版本等于**作废所有上游缓存**）在 TTL 内钉在同一个账号上，保住上游 prompt cache；账号不可用时才换并更新钉子，失败换号后解除钉子。`0` = 关闭粘性（每轮独立选号）。**TTL 只是安全上界**，正常回收靠容量 LRU（`STICKY_MAX = 4096`，最久未用先走）——上游 prompt cache（DeepSeek）保留是小时级，TTL 太短会让"停一会儿回来"的会话丢掉仍然有效的缓存。运行计数见下方「会话粘性计数」 |
| `request_log_path` | string | `"~/.dsh/opencode-pool/requests.jsonl"` | **0.2.0 新增**：结构化请求日志（一行一个 JSON、一个请求一行），见「结构化请求日志」。`""` = 关闭；相对路径按配置目录解析 |
| `request_log_max_bytes` | int | `20971520`（20 MB） | 日志超过这个大小就轮转成 `.1` / `.2` / … |
| `request_log_keep` | int | `3` | 轮转后保留的历史份数 |
| `providers` | object | — | 上游接入，见下 |
| `accounts` | list | `[]` | 账号（凭据），见下。空列表启动不报错，但任何请求都会 503 |

### 会话粘性计数（`/v1/usage` 的 `sticky`）

`GET /v1/usage` 的响应里带一个 `sticky` 对象，反映粘性的运行状态：

```json
"sticky": { "ttl_seconds": 43200, "pinned": 3, "hits": 128, "misses": 17 }
```

| 字段 | 怎么读 |
| --- | --- |
| `ttl_seconds` | 当前生效的 TTL（等于 `sticky_ttl_seconds`；`0` = 粘性已关闭，计数不再增长） |
| `pinned` | 当前钉住的对话数（映射表按 TTL 清理 + `STICKY_MAX` 容量 LRU 淘汰，不会无限增长） |
| `hits` | 粘性命中次数：对话被钉回原账号 |
| `misses` | 粘性未命中次数：新对话 / 钉子过期 / 原账号不可用而重选 |

`hits` 长期远小于 `misses` 通常意味着账号频繁不可用（反复重选）或 TTL 太短。
同一组计数也以 `pool_sticky_hits_total` / `pool_sticky_misses_total` 出现在
`/metrics` 里（见 [troubleshooting.md](troubleshooting.md)）。

另外每个账号条目还带三个**缓存复用**字段（粘性到底有没有保住缓存，看这三个）：

| 字段 | 怎么读 |
| --- | --- |
| `cache_read_tokens` | 上游 prompt cache 命中的输入 token 累计（`usage.opencode.tokens.cache_read`） |
| `input_tokens` | **未命中缓存**、真正送进模型的输入 token 累计（`usage.opencode.tokens.input`） |
| `cache_hit_ratio` | `cache_read_tokens / (cache_read_tokens + input_tokens)`，无输入时为 `null`；恒 ≤ 1 |

> 口径说明：上游的 `input` 是"未命中部分"，与 `cache_read` 是**互补**的两个分量
> （实测热请求 `input=58, cache_read=2176`，两者之和 = 总 prompt 2234）。所以命中率
> 分母必须两者相加；旧口径 `cache_read / input` 会算出 > 1 的无意义比值。
> **什么时候接近 1**：同一对话持续复用前缀时（多轮长对话的典型形态），
> `input` 只剩新增部分，比值会逼近 1；新对话首轮、短 prompt（实测 43 token
> 不命中缓存）时它天然接近 0。**不要拿它当成本节省率**：它衡量的是前缀复用
> 程度，不是计费折扣 —— 上游怎么折算成本是另一回事。

> **本机缓存存活实测（2026-11-06，本机）**：同一 2234 token 前缀、代码版本不变，
> 基线命中 2176；**+6 分钟再发仍命中、+20 分钟再发仍命中**。对照各家口径：
> DeepSeek 官方「几小时到几天」、Anthropic 5 分钟、OpenAI 5–10 分钟。
> 结论：**12 小时 TTL 只是安全上界，"命中即续期"才是保住缓存的关键**
> （每次命中把钉子续到 `now + TTL`，见 `Pool.pick`）。
> ⚠️ 更早一轮「36 分钟缓存失效」的观察是**指纹公式变更**（`conversation_seed`
> 口径调整）造成的假象，请勿引用。

读法与排障见 [troubleshooting.md](troubleshooting.md) 的 `/metrics` 节。

## 结构化请求日志（`requests.jsonl`）

`request_log_*` 三个字段控制的是一份**结构化事件流**：append-only、一行一个 JSON、
每个客户端请求恰好一行。它与给人读的散文日志（`relay.log` / stdout 的 `[req]` 行）
**并存**——排障看 `[req]`，做统计与审计吃 `requests.jsonl`（`jq` / `pandas` /
[scripts/stats.py](../scripts/stats.py) 都能直接解析）。`request_log_path: ""` 即关闭。
日志**不含** prompt / response 正文与 Authorization（见 SECURITY.md）。

字段清单（21 个顶层键；缺失一律 `null`，**不省略键、也不用 0 顶替**）：

| 字段 | 含义 |
| --- | --- |
| `ts` | 本地时区 + 毫秒 ISO8601（如 `2026-11-06T17:40:12.345+08:00`） |
| `req_id` | 8 位随机十六进制，**不派生自请求内容**（隐私）；见下方语义 ① |
| `method` / `path` / `model` / `stream` | 请求坐标；`stream` 为布尔 |
| `account` / `provider` | 实际接单的账号与其 provider（没走到选号时为 `null`） |
| `status` / `upstream_status` | 回给客户端的状态码 / 上游原始状态码 |
| `outcome` | `ok` / `failover` / `throttled` / `client_error` / `passthrough` / `no_account` |
| `queue_ms` | 本地耗时（排队、并发闸、重试退避），见语义 ② |
| `ttfb_ms` | 上游首字节耗时（非流式等于 `total_ms`），见语义 ② |
| `total_ms` | 端到端总耗时 |
| `bytes` | 响应体大小 |
| `retries` / `failovers` | 同号重试次数 / 换号次数 |
| `sticky` | `off`（粘性关闭）/ `hit` / `miss` |
| `tokens` | 子对象：`input` / `cache_read` / `output` / `reasoning` / `cache_write_5m` / `cache_write_1h`；见语义 ③ |
| `allowance` | 上游计费量（`usage.opencode.allowance`），缺失 `null` |
| `error_reason` | 失败原因（`queue-timeout` / `missing-key` / `concurrency-timeout` / `net:<异常>` …），成功为 `null` |

三个容易记反的语义：

1. **`req_id` 贯穿全程**：一次客户端请求只写一行，它的排队、同号重试、换号尝试
   都归在这一个 id 下；散文日志里的 `req=<id>` 标签就是它，两边可以互相对照。
2. **`queue_ms` 归本地、`ttfb_ms` 归上游**：慢在 `queue_ms` = 池子在排队
   （并发闸 / 冷却等待 / 重试退避），是**本地瓶颈**；慢在 `ttfb_ms` = 上游出字慢。
3. **`tokens` 缺失写 `null` 不写 0**：`0` 是真实读数（真的读到 0），
   `null` 是"上游没给"——统计口径别混（`scripts/stats.py` 按这个口径算命中率）。

轮转：超过 `request_log_max_bytes` 切成 `requests.jsonl.1` / `.2` / …，
只留 `request_log_keep` 份历史。

分析用法（`stats.py` 与每个数怎么读）见 [analytics.md](analytics.md)。

## `providers`：一次上游接入

key 是 provider id（自定，账号用它来引用），值是一个对象：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `adapter` | string | `"openai-compatible"` | 适配器 id：内置 `opencode-go` / `openai-compatible`，或 `adapters/ext/` 里的自定义 id |
| `base_url` | string | 适配器默认 | 上游 base。`opencode-go` 默认 `https://opencode.ai/inference/go/v1`；`openai-compatible` **必填** |
| `models` | list | `[]` | 该上游认领的模型白名单；空 = 适配器声称的全部。声明了具体模型的 provider 优先于写 `"*"` 或没写的（显式优先路由） |
| `headers` | object | `{}` | 发往该上游的固定请求头（叠加在适配器生成的头之上） |
| `options` | object | `{}` | 透传给适配器的自由参数（合并进账号视图，账号级同名字段优先） |

```json
"providers": {
  "local-vllm": {
    "adapter": "openai-compatible",
    "base_url": "http://127.0.0.1:8794/v1",
    "models": ["qwen3-32b"],
    "headers": {"X-Tenant": "lab"}
  }
}
```

## `accounts`：N 把凭据

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 必填 | 账号名，`[A-Za-z0-9_.-]{1,40}`，全池唯一 |
| `provider` | string | 第一个 provider | 指向哪个 provider |
| `key_env` | string | 自动生成 | 凭据槽位名：按下面的解析顺序取 API key。面板加号时留空会自动生成 `POOL_KEY_<NAME大写>` |
| `enabled` | bool | `true` | 暂停 / 恢复该账号（面板胶囊可切） |
| `session_prefix` | string | `dsh-pool-<name>` | （`opencode-go`）`x-opencode-session` 的前缀，每号一个，用于 prompt cache 亲和域 |
| `headers` | object | `{}` | 账号级请求头覆盖（同名覆盖 provider 级） |
| `base_url` | string | provider 级 | 账号级 base 覆盖（同一个 provider 下个别账号走不同入口时用） |

```json
"accounts": [
  {"name": "go01", "provider": "opencode-go", "key_env": "OPENCODE_KEY_01",
   "session_prefix": "dsh-pool-01"}
]
```

## 密钥从哪来（`key_env` 的解析顺序）

`key_env` 存的不是密钥，是**槽位名**。取值按优先级：

1. **环境变量**：进程环境里有同名变量就用它（面板改不动这种，显示 `source: env`）；
2. **面板密钥仓** `secrets_file`（`0600`）：面板写的一律落这里（`source: pool`）；
3. **DSH `.credentials.yaml` 的 `refs:` 段**（只读回退，`source: dsh`）：
   DSH 里已有的 key 照样能直接跑，但面板不会去改它。

API 只回 `"来源 + 8 位指纹"`（SHA-256 前 8 位，不可逆），任何端点都不回显明文。

## 面板改配置的行为（写回语义）

- 面板的加号 / 换 key / 删号 / 暂停操作会**写回 `Config.source_path`**
  （即这份池子自己读的那份配置，不是写死默认路径）；
- 写之前先用校验器验证，验证不过**不落地**（返回 400 + 原因）；
- 每次写前自动备份为 `<配置>.bak-<时间戳>-panel`，写入用 `tmp + rename` 原子替换；
- 删号时若没有别的账号再用同一槽位，会把密钥从密钥仓一起回收
  （响应里 `secret_dropped: true`）。

## 相对路径怎么解析

`state_file` / `pid_file` / `credentials_yaml` / `secrets_file` 写相对路径时，
按**配置文件所在目录**解析（不是当前工作目录）。`init` 生成的模板就是这么写的：
`pool_state.json` 与 `relay.pid` 落在 `~/.dsh/opencode-pool/` 里。
`~` 开头的路径按运行用户展开 —— systemd 下要写绝对路径（unit 里的
`Environment=OPENCODE_POOL_CONFIG` 不做 `~` 展开）。

## 未填写时的兜底（Config 兜底 vs init 模板）

两套默认值都合法，但来源不同，别混淆：

- **配置文件里省略**某字段时，用 `Config` dataclass 的兜底值；
- **`python3 -m opencode_pool init`** 生成的配置文件会把这些字段**显式写出来**，
  给的是更完整的另一套值（方便开箱即用）。

有差异的是这三个字段：

| 字段 | 配置里省略时（Config 兜底） | `init` 模板显式写入 |
| --- | --- | --- |
| `state_file` | `""`（**不**持久化冷却/累计状态） | `"pool_state.json"`（相对配置目录解析，落在 `~/.dsh/opencode-pool/`） |
| `pid_file` | `""`（**不**写 pid 文件） | `"relay.pid"` |
| `credentials_yaml` | 内置的本机绝对路径（`~/.dsh/.credentials.yaml`） | `~/.dsh/.credentials.yaml` 按运行用户展开后的绝对路径 |

也就是说：**手写配置想开箱即用，就照 init 模板把 `state_file` / `pid_file` 显式写上**，
否则重启后冷却与累计不保留、`start.sh` 的 pid 判断退化为纯端口探测
（`credentials_yaml` 两者在本机展开结果相同，仅写法不同）。

另外：`request_log_*` 三项**不在** init 模板里 —— 省略即用 Config 兜底
（日志默认**开启**在 `~/.dsh/opencode-pool/requests.jsonl`）；不想要就显式写
`"request_log_path": ""`。

## 校验器会报什么（`check` / 面板写入前）

- provider 引用了不存在的适配器（顺带列出已加载的 id，含 `adapters/ext/` 的加载错误）；
- provider 缺 `base_url` 且适配器也没有默认值；
- 账号缺 `name` / 重名 / 引用的 provider 不存在；
- 适配器声明的必填字段没填（如 `key_env`）；
- 没有任何 account（提示，不阻断启动）。

```bash
python3 -m opencode_pool check
# 配置 OK · version=2 · 适配器=['opencode-go', 'openai-compatible']
#   provider opencode-go: adapter=opencode-go base=https://opencode.ai/inference/go/v1
#   account go01: provider=opencode-go key_env=OPENCODE_KEY_01
```
