# 架构与扩展点

这份文档回答一个问题：**下一个号源、下一档额度、下一种失败语义，要改哪里？**

设计目标：调度器与面板不认识任何具体上游。所有"上游差异"必须能被三种人用三种成本接入——
改配置的人（0 行）、写适配器的人（1 个文件）、改核心的人（应该不需要）。

## 1. 分层

```
                     ┌──────────────────────────────┐
  OpenAI 客户端 ───▶ │ server.py                    │  只做：选号 → 换号 → 透传
                     │  /v1/chat/completions …      │
                     └──────────────┬───────────────┘
                                    │ ① pick(model) ② build_headers ③ classify
                     ┌──────────────▼───────────────┐
                     │ pool.py                      │  headroom / 冷却 / 记账 / 状态
                     └──────────────┬───────────────┘
                                    │ 声明：Field[] / WindowSpec[] / supports_quota
                     ┌──────────────▼───────────────┐
                     │ adapters/*.py                │  差异全部在这里
                     └──────────────────────────────┘
```

三条不变式：

1. **池只认三类东西**：窗口百分比、冷却截止时间、账号健康。任何上游都必须能折算成这三样，
   折算不了的（没有余量查询）就显式退化为轮询 —— 不假装知道。
2. **失败处置是数据，不是分支**：适配器的 `classify()` 返回 `ErrorDecision`，调度器照着执行，
   核心代码里没有 `if provider == "opencode"`。
3. **面板只读 `/v1/pool/schema`**：窗口的 key / 标签 / 配色 / 数量、适配器的能力位都在里面，
   加一档窗口不用碰 HTML。

## 2. 学到的三个模式

| 项目 | 它的扩展机制 | 本实现借了什么 |
| --- | --- | --- |
| **gpt-load** | `spec.Module = Definition{ID, Name, Connection, Params[], Credentials[], Provider, Routes[协议×操作×处理方式], Capabilities}` —— 声明式，UI 由字段描述生成；额度历史是 `window{key, label, scope, window_seconds} + points{observed_at, used, reset_at}` | `WindowSpec(key,label,scope,window_seconds,hue)`、`Field`、`capabilities(supports_quota)`、历史采样点 |
| **new-api** | `relay/channel.Adaptor` 接口：`GetRequestURL` / `SetupRequestHeader` / `ConvertXxxRequest` / `DoResponse`（返回 usage） | `endpoint()` / `build_headers()` / `map_path()` / `extract_usage()` 四件套 |
| **LiteLLM** | provider 注册表 + 自定义 handler，同一入口多种 deployment、fallback 链 | 多 provider 同入口 + 显式模型优先的路由 |

一句话：**声明在前，行为在后，注册表发现，引擎通用**。

## 3. 三条接入路径

### 3.1 只改配置：接一个任意 `/v1` 上游（0 行代码）

```json
"providers": {
  "local-vllm": { "adapter": "openai-compatible", "base_url": "http://127.0.0.1:8794/v1",
                  "models": ["qwen3-32b"], "headers": {"X-Tenant": "lab"} }
},
"accounts": [
  { "name": "vllm-01", "provider": "local-vllm", "key_env": "VLLM_KEY" }
]
```

行为：Bearer 鉴权、无余量观测 → 面板显示「无额度观测 / 按轮询调度」，调度退化为轮询 + 429/5xx 冷却。
本文档写完后实测过：本地假上游的 `qwen3-32b` 与 opencode 的 `deepseek-v4.1-flash` 同一个入口并存，
各自路由到正确的上游。

### 3.2 写一个适配器：上游有特殊鉴权 / 特殊额度（1 个文件）

丢进 `relay/adapters/ext/`（启动自动加载，崩了只记 `adapter_load_errors`，不影响主进程）：

```python
from relay.adapters.base import Adapter, Field, WindowSpec, ErrorDecision, WindowState, QuotaSnapshot

class MyUpstream(Adapter):
    id = "my-upstream"
    display_name = "My Upstream"
    base_url = "https://api.example.com/v1"
    supports_quota = True
    fields = (Field("key_env", "API 密钥", kind="secret", required=True, sensitive=True),)
    windows = (WindowSpec("daily", "日", scope="periodic", window_seconds=86400, hue="var(--w5)"),)

    def build_headers(self, account, key, inbound):
        return {"Authorization": f"Bearer {key}", "X-App": "relay"}

    def probe(self, account, key, http):
        data = http.get(f"{self.endpoint(account)}/quota", headers={"Authorization": f"Bearer {key}"}).json()
        return QuotaSnapshot(observed_at=__import__("time").time(),
                             windows={"daily": WindowState(percent=data["used_pct"], status="ok",
                                                           resets_at=data["reset_at"])})

    def classify(self, status, body, headers, account):
        if status == 429:
            return ErrorDecision("failover", scope="window", window="daily", reason="quota:daily")
        if status == 401:
            return ErrorDecision("failover", 1800, reason="auth:401")
        return ErrorDecision("passthrough") if status < 500 else ErrorDecision("failover", 30)

    def extract_usage(self, payload):
        return {"tokens": (payload or {}).get("usage")}

ADAPTER = MyUpstream()
```

然后在配置里 `"adapter": "my-upstream"`。**面板、调度、状态持久化、`/v1/usage` 全部自动适配**，
因为窗口是从 `windows` 声明的，不是写死的。

### 3.3 改核心（预期用不到）

只有当你需要**跨账号**的新概念（例如"同一次请求必须落在同一账号的会话粘性"）才需要动 `pool.py`。
`ErrorDecision` 已经预留了 `action="retry"` 与 `scope` 两个维度，多数新语义能塞进现有形状。

## 4. 常见扩展问答

**加第四档窗口（比如"日"）？**
在适配器的 `windows` 加一项即可：调度自动把它算进 `headroom = min(...)`，面板自动多画一个环，
`/v1/usage` 自动多一个 key，历史采样自动开始记录。零改动。

**两个上游的窗口语义完全不同（一个是滚动 5h，一个是日预算）？**
没关系，窗口是本地的概念：调度只看百分比与重置时刻。`scope` / `window_seconds` 只用于展示与图表。

**想让某个模型优先走贵但稳的上游？**
两个 provider 都写 `models: ["gpt-6-luna"]`，余量高的赢；想强制优先级就给稳定上游更高的初始余量
（现在没有权重字段，是刻意不加——先有真实需求再加，避免没人用的旋钮）。

**上游的 429 不是"额度打满"而是"并发太高"？**
在 `classify()` 里区分：额度打满 → `scope="window"` + 冷到重置；并发太高 → `cooldown_seconds=10`。
这两种在 opencode 适配器里已经分开了（`limitName` 有无决定 scope）。

**要不要给每个账号加"权重/并发上限"？**
当前 `headroom` 就是权重：余量大的多接。并发上限属于「同一账号同时打几个请求」的策略，
需要新增 `max_inflight` 字段与计数，属于 3.3 类改动；真需要时按 `Account` 加字段 + `pick()` 过滤即可。

## 5. 校验与测试

```bash
python3 -m opencode_pool check           # 配置校验：适配器存在？字段齐？provider 引用对？
python3 -m unittest discover -s tests    # 回归用例（数量与结果以 tests/ 与 CI 输出为准）
```

测试覆盖的四条线（新增适配器时照抄改名即可）：

| 线 | 用例 |
| --- | --- |
| 声明 | 内置适配器已注册、descriptor 带窗口与敏感字段 |
| 配置 | v1 自动迁移、未知适配器/未知 provider 被点名 |
| 判定 | 429 limitName → 窗口级、403 透传、401 冷却 |
| 调度 | 余量最大者胜出、打满窗口被跳过、无观测上游走轮询、冷到重置 |
| 端到端 | 假上游先 429 再 200 → 客户端拿到 200 且第一个账号进入冷却 |

## 6. 配置与密钥的写入（面板即控制面）

面板能加号/换 key/删号，代价是要把"写"这件事做对。三条规则：

**① 写回本实例自己读的那份配置。**
`Config.load()` 记下 `source_path`，写入走 `config_path(pool)`。
踩过的坑：第一版把路径写死成 `HERE/pool.config.json`，用 `/tmp/test.config.json` 起的测试实例
一改配置就覆盖了生产文件（测试还"通过"了，因为它读回的正是自己刚写的生产文件）。现在有回归用例守着：
测试实例加号后断言生产配置一字未动。

**② 先验证、先备份、再原子写。**
`write_config(mutator)` 的流程是：读原文件 → 改 → `Config.from_dict(raw).validate()` →
不过就抛错不落地 → `cp` 一份 `*.bak-<时间戳>-panel` → 写 `.tmp` → `os.replace`。
所以任何一次面板操作都能用一行 `cp` 回滚。

**③ 密钥与配置分开存，且永不回显。**
- 位置：`~/.dsh/opencode-pool/secrets.json`，目录 0700 / 文件 0600，**在工作区之外**
  （工作区是 git 仓库，密钥放进 `pool.config.json` 就等于进版本库）
- 解析顺序：环境变量 → 密钥仓 → DSH `.credentials.yaml`（只读回退）；后写的优先级高，但
  **环境变量来源的槽位拒绝覆盖**——那是进程外的事实，面板不该假装改得动
- 回显：API 只返回 `{configured, source, fingerprint(sha256 前 8 位), editable}`；
  明文只在上游请求头里出现，日志只记账号名与原因
- 回收：删号时若没有别的账号再用该槽位，密钥一起删；缓存按 mtime 失效，避免多实例看到旧值

**为什么允许面板写密钥？** 因为面板只对 loopback 提供、token 由服务端注入页面、且这个池子本来就是
单机自用。要放到局域网给多人用，第一件事应该是把写入路径关掉（删掉三个 POST 路由即可）。

## 7. DSH 插件的三个 id（血的教训）

`package.json.name`、`cordis.patch.yml` 的 `insert[].id`、客户端 `__ModuleLoader__.load({ id })`
是**三个不同的东西**，其中只有"包名"和"模块 id"必须相等：

- 客户端 `id` 写成 cordis 条目 id → loader 找不回包 → `could not load … without registering`
- 而 DSH 的 web boot 是全有全无：一个客户端条目挂了，整页停在 `Failed to load plugins`
  （每次点名哪个插件可能不同，所以看着像"随机插件坏了"）

判据：报错带 `without registering` = id 不对/没包 `__ModuleLoader__.load`；
报错只有 `X: failed` = 插件 `apply()` 抛错。两条已在 `tests/plugin.test.mjs` 里做了断言。

顺带一条同类教训：**CLI 的 `--config` 必须在两个位置都接受**
（`--config X serve` 与 `serve --config X`）。第一版只注册在顶层，插件和 `start.sh`
用的是后者，于是拉起的进程秒退、面板 502，而日志里只有一行 `unrecognized arguments`。

## 8. 已知边界（有意接受）

- 单进程、单机、本地 JSON 状态；多机要么各跑一套，要么换共享存储
- 不做上游能力探测：没声明的模型交给上游 4xx 原样返回
- 不支持 Google 原生协议（Gemini 走 OpenAI 协议已验证可通）
- 不打乱 prompt cache：会话头按「账号前缀 + 会话哈希」稳定生成，换号会换缓存域——这是多号池的固有代价
