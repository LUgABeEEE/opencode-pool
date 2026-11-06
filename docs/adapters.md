# 适配器：接一个新号源

设计不变式：**上游差异全部进 `opencode_pool/adapters/`，调度器与面板不认识任何具体上游。**
一个新号源 = 一份声明（字段 / 窗口）+ 四个行为（请求头 / 余量探测 / 失败判定 / 用量解析）。

## 三条接入路径（按成本从低到高）

1. **纯配置（0 行代码）**：任意 `/v1` 兼容上游，配置里加一段
   `providers`（`adapter: "openai-compatible"`）即可 —— Bearer 鉴权、
   无余量观测、429/5xx 冷却、轮询调度。见 [configuration.md](configuration.md)。
2. **掉落适配器（1 个文件）**：上游有特殊鉴权 / 特殊额度语义时，
   写一个 `.py` 丢进 `opencode_pool/adapters/ext/`，启动自动加载（见下）。
3. **内置适配器**：像 `opencode.py` 一样放进 `opencode_pool/adapters/`
   并在 `adapters/__init__.py` 的 `load_builtins()` 列名 —— 这是往上游贡献的形态。

## Adapter 契约

类属性（声明，面板 / CLI / 校验器直接读）：

| 属性 | 类型 | 说明 |
| --- | --- | --- |
| `id` | str | 全局唯一；配置里 `providers.<pid>.adapter` 引用它 |
| `display_name` / `description` | str | 面板展示 |
| `base_url` | str | 上游默认 base（配置可覆盖） |
| `protocols` | tuple | `openai-completions` / `anthropic-messages` / `openai-responses` … |
| `fields` | `Field[]` | 账号要填的字段声明（`key_env` 等）；`sensitive=True` 的面板打码 |
| `account_fields` | `Field[]` | 账号级可选字段（如 `session_prefix`） |
| `windows` | `WindowSpec[]` | 额度窗口声明：`key/label/scope/window_seconds/hue`。**没有声明就没有额度环**；调度自动按 `headroom = min(100 - percent)` 折算 |
| `supports_quota` | bool | `False` = 上游不可观测余量，调度显式退化为轮询 + 冷却 |

方法（行为）：

| 方法 | 什么时候被调 | 约定 |
| --- | --- | --- |
| `endpoint(account)` | 每次请求 | 一般不用重写；`base_url` 取 `account.base_url or provider.base_url or 适配器默认` |
| `build_headers(account, key, inbound, body=None)` | 每次请求 | 返回发往上游的请求头；`inbound` 是客户端原始头（小写键），`body` 给"按对话派生状态"的适配器用 |
| `map_path(path)` | 每次请求 | 客户端路径 → 上游路径；默认剥掉 `/v1` 前缀（假设 base 已带 `/v1`） |
| `probe(account, key, http)` | 每 `usage_refresh_seconds` | 查余量，返回 `QuotaSnapshot`（`{window_key: WindowState}`）；`supports_quota=True` 必须实现 |
| `classify(status, body, headers, account)` | 上游回 ≥400 | **抽象方法**，必须实现。返回 `ErrorDecision`，调度器只执行不判断 |
| `extract_usage(payload)` | 成功回包后 | 从响应里抠用量（如 `usage.opencode.allowance`），进累计账 |
| `validate(account)` | 配置加载 / 面板写入前 | 默认按 `fields` 的 `required` 校验，可扩展 |
| `descriptor()` | `/v1/pool/schema` | 一般不用重写；面板据此渲染加号表单与额度环 |

两个数据类：

```python
ErrorDecision(action,            # "failover" 换号 | "passthrough" 原样回客户端 | "retry" 同号短退避重试
              cooldown_seconds,  # failover 时的兜底冷却
              scope,             # "account" | "window"
              window,            # scope="window" 时打满的是哪档（如 "weekly"）
              until,             # 绝对时间戳（额度打满时给窗口重置时刻）
              reason, detail)    # reason 进日志与指标；detail 只进日志
QuotaSnapshot(windows={key: WindowState(percent, status, resets_at, reset_raw)}, observed_at)
```

失败语义速查（内置适配器的做法，写新适配器时可参考）：

| 上游返回 | 处置 | 为什么 |
| --- | --- | --- |
| 429 + 额度窗口（`limitName`） | `failover, scope="window"`，冷到窗口重置 | 这个号真打满了，换号是对的 |
| 429 无窗口（并发/瞬时限流） | `retry` 同号短退避 | 额度还在，冷账号是错的（唯一号被冷整池 503） |
| 401 | `failover`，冷却 30 分钟 | key 坏了，大概率不是瞬时的 |
| 5xx | `failover`，冷却 30 秒 | 上游抖动，躲一下 |
| 其他 4xx（403 地区限制、400 参数错） | `passthrough` | 换号没用，原样回给客户端，不消耗冷却 |

## `adapters/ext/` 掉落目录

把实现 `Adapter` 的 `.py` 丢进 `opencode_pool/adapters/ext/`（文件名不以 `_` 开头），
启动时自动发现并登记。两种暴露方式任选：

```python
# 写法一：暴露 ADAPTER 变量
ADAPTER = MyAdapter()

# 写法二：暴露 register(registry) —— 想注册多个时用
def register(registry):
    registry.register(MyAdapterA())
    registry.register(MyAdapterB())
```

约定与注意：

- 加载失败**不会**拖垮主进程：错误记进 `/v1/pool/schema` 的
  `adapter_load_errors`，其余适配器照常工作；
- 导入请用绝对路径 `from opencode_pool.adapters.base import ...`
  （掉落文件不是包内模块，相对导入 `from ..base` 在这里解析不了）；
- `id` 必须唯一，重复注册会覆盖。

## 最小适配器示例（可直接 py_compile）

单档预算的 OpenAI 兼容上游：Bearer 鉴权、`/quota` 查用量、429/401/5xx 各给出处置。
存成 `opencode_pool/adapters/ext/budget_echo.py` 重启服务即生效，
配置里 `"adapter": "budget-echo"`。

```python
"""最小适配器示例：单档预算、Bearer 鉴权的 OpenAI 兼容上游。"""
import time

from opencode_pool.adapters.base import (
    Adapter,
    ErrorDecision,
    Field,
    QuotaSnapshot,
    WindowSpec,
    WindowState,
)


class BudgetEchoAdapter(Adapter):
    id = "budget-echo"
    display_name = "Budget Echo"
    description = "示例：单档预算的 OpenAI 兼容上游"
    base_url = "http://127.0.0.1:9900/v1"
    supports_quota = True

    fields = (
        Field("key_env", "API 密钥的环境变量名 / 密钥仓槽位", kind="secret",
              required=True, sensitive=True),
        Field("base_url", "Base URL", kind="url", required=True),
    )
    windows = (
        WindowSpec("budget", "总预算", scope="budget", window_seconds=None),
    )

    def build_headers(self, account, key, inbound, body=None):
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def probe(self, account, key, http):
        resp = http.get(
            f"{self.endpoint(account)}/quota",
            headers={"Authorization": f"Bearer {key}"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json() or {}
        return QuotaSnapshot(
            observed_at=time.time(),
            windows={
                "budget": WindowState(
                    percent=float(data.get("used_percent", 0)), status="ok"
                )
            },
        )

    def classify(self, status, body, headers, account):
        if status == 429:
            return ErrorDecision("failover", cooldown_seconds=60, reason="rate-limited")
        if status == 401:
            return ErrorDecision("failover", cooldown_seconds=1800, reason="auth:401")
        if status >= 500:
            return ErrorDecision("failover", cooldown_seconds=30,
                                 reason=f"upstream:{status}")
        return ErrorDecision("passthrough", detail=f"client:{status}")


ADAPTER = BudgetEchoAdapter()
```

验证：

```bash
python3 -m py_compile opencode_pool/adapters/ext/budget_echo.py   # 语法
python3 -m opencode_pool check                                     # 注册成功、配置认得它
```

## 加了适配器，面板 / 调度 / 端点为什么自动就对了

- 面板只读 `/v1/pool/schema` 的 `descriptor()`：窗口的 key / 标签 / 配色 / 数量
  全在声明里，加一档窗口（比如"日"）只需要 `windows` 加一项 ——
  调度自动把它算进 `headroom`，面板自动多画一个环，历史采样自动多一条曲线；
- `/v1/usage` 的账号视图按 `windows` 声明逐档输出；
- 调度只看三个值：窗口百分比、冷却截止、账号健康 —— 任何上游都必须折算成这三样。

写完适配器想贡献回来：见 [../CONTRIBUTING.md](../CONTRIBUTING.md)
（至少带 `classify` / `probe` / `validate` 三类用例）。
