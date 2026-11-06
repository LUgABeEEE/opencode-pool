# opencode-pool

**给 OpenCode Go 订阅做的号池**：把多个自己付费的订阅账号合成**一个** OpenAI 兼容入口 ——
按三档额度窗口（5 小时 / 周 / 月）选号、超额自动换号、会话粘性保住 prompt cache，
自带控制台与 Prometheus 指标。

[![ci](https://github.com/LUgABeEEE/opencode-pool/actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 它解决什么

OpenCode Go 的额度不是"一个账号一个余额"，而是**三档窗口**各自计数，打满后 429
会一直持续到窗口重置（实测周额度打满要冷 100 小时以上）。一机多订阅时：

- 轮询会把请求打到已经打满的号上 → 白白失败；
- 同一对话被摊到不同账号 → 上游 prompt cache 每轮失效，"深度思考很久不出字"；
- 哪个号快死了、什么时候回血，看不见。

这个池子把"哪个号现在能用、还能用多久"变成带时间语义的状态，对外只暴露一个端点。

## 架构

```
 客户端（OpenAI SDK / Claude Code / Codex / 任意）
   │  POST /v1/chat/completions · /v1/messages · /v1/responses
   ▼
┌────────────────────────────────────────────────────────────┐
│ server.py     选号 → 注入会话头 → 失败换号 → 透传 / 流式    │
│               /  面板(仅 loopback) · /healthz · /metrics    │
└──────────────┬─────────────────────────────────────────────┘
               │ ① sticky 对话钉号 ② pick(model, headroom)
┌──────────────▼─────────────────────────────────────────────┐
│ pool.py       调度核心：余量折算 / 冷却 / 记账 / 状态持久化  │
│               （不认识任何具体上游）                        │
└──────────────┬─────────────────────────────────────────────┘
               │ 声明：Field[] · WindowSpec[] · supports_quota
┌──────────────▼─────────────────────────────────────────────┐
│ adapters/     上游差异全部在这里：opencode-go ·             │
│               openai-compatible · ext/ 掉落目录             │
└──────────────┬─────────────────────────────────────────────┘
               ▼
        上游账号 ×N   ──  /v1/usage 轮询三档额度
```

三条不变式：**差异是数据**（字段/窗口声明）、**失败处置是数据**（`ErrorDecision`）、
**面板只读 schema**（`/v1/pool/schema`）。细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 快速开始

```bash
python3 -m pip install requests          # 唯一运行时依赖
python3 -m opencode_pool init            # 生成 ~/.dsh/opencode-pool/pool.config.json（含 relay token）
python3 -m opencode_pool serve           # 起服务，默认 127.0.0.1:8790
```

打开 <http://127.0.0.1:8790/> 就是控制台：**加号 → 填 key → 看三档额度环**。
客户端把 `base_url` 指到 `http://127.0.0.1:8790/v1`、API key 填 `relay_token` 即可。

```bash
python3 -m opencode_pool status          # 看池子：余量 / 绑定窗口 / 冷却原因
python3 -m opencode_pool check           # 改完配置先校验
```

DSH 用户装插件（侧边栏面板、局域网可用、token 服务端注入）：

```bash
dsh plugin add /path/to/opencode-pool/dsh-plugin
```

更多：**[docs/quickstart.md](docs/quickstart.md)**（5 分钟完整跑通）。

## 功能

| | 功能 | 说明 |
| --- | --- | --- |
| 🎯 | 余量感知选号 | `headroom = min(100 − 各窗口 percent)`，贴死线的号不接单（`min_headroom` 水位线） |
| 🔁 | 失败自动处置 | 429 按**窗口**冷到重置、瞬时限流**同号指数退避重试**、401 冷 30 分钟、5xx 短冷却、4xx 原样透传 |
| ⚙️ | 并发与速率保护 | 每号并发位（`max_inflight`）+ 最小间隔（`min_interval_seconds`）；全池限流时先排队（`queue_wait_seconds`）再 503 |
| 📌 | 会话粘性 | 同一对话钉在同一账号，保住 prompt cache；TTL 默认 12 小时（只是安全上界，超容量按 LRU 淘汰）；失败换号自动解钉 |
| 📊 | 三档额度观测 | `/v1/usage` 轮询 5h / 周 / 月窗口，速率与耗尽时间推算 |
| 🖥 | 控制台 | 窗口卡 / 三环仪表 / 寿命带 / 加号·换 key·删号·暂停 / 热重载配置 |
| 📈 | 可观测性 | `/metrics`（Prometheus，含 prompt cache 命中指标）、`/healthz` 就绪探针（无可用账号或采样过期回 503） |
| 📝 | 结构化请求日志 | `requests.jsonl` 逐请求事件流（21 字段、缺失写 `null`、按大小轮转）+ 零依赖分析脚本 `scripts/stats.py` |
| 🔌 | 适配器扩展 | 配置级接入（0 行）→ `adapters/ext/` 掉落（1 个文件）→ 内置 |
| 🔒 | 安全边界 | 面板仅 loopback、密钥仓 0600、任何接口不回显密钥、写配置先校验先备份 |
| 🧰 | 工程完备 | CI（Python 3.10–3.12 × Node 20/22）、Docker / compose、systemd unit、Windows 任务计划、回归测试（结果以 CI 输出为准） |

## 端点

| 路径 | 说明 |
| --- | --- |
| `POST /v1/chat/completions` · `/v1/messages` · `/v1/responses` | 兼容入口（流式支持） |
| `GET /v1/models` · `/v1/usage` | 模型目录 / 池状态 |
| `GET /v1/pool/schema` | 已加载的适配器与窗口声明（面板据此渲染） |
| `POST /v1/pool/accounts` · `/account/key` · `/account/remove` | 加号 / 换 key / 删号 |
| `POST /v1/pool/refresh` · `/clear-cooldowns` · `/reload` | 立刻查额度 / 清冷却 / 热重载配置 |
| `GET /` · `/healthz` · `/metrics` | 控制台 / 就绪探针 / Prometheus 指标 |

## 和其他项目的关系

都是"把 AI 服务聚成一个入口"，但定位不同（以各项目公开 README 为准，2026-11）：

| 项目 | 它是什么 | opencode-pool 的差异 |
| --- | --- | --- |
| [opencode2api](https://github.com/super-mortal/opencode2api) | 协议转换网关：把 OpenCode 变成标准 API（模型别名、代理、面板） | 我们不做协议翻译的花样，做**多订阅账号池**：额度窗口选号、按窗口冷却、sticky 保缓存。sticky 与 `/metrics` 的设计借鉴了它 |
| [Cline-proxy](https://github.com/YuJunZhiXue/Cline-proxy) | Go 写的 Cline 账号池反代（多账号轮询、双协议、管理后台） | 它面向 Cline 账号池；我们面向 OpenCode Go 订阅的**三档额度窗口**语义，选号看余量而不是纯轮询 |
| [gpt-load](https://github.com/tbphp/gpt-load) | 通用 API key 池：分组聚合、模型路由、加权负载均衡 | 它是通用 key 池/流量调度；我们专精 OpenCode 订阅的额度观测与失败语义，声明式字段/窗口的设计借鉴了它的 spec 模式 |
| new-api | LLM API 网关/中转站（渠道管理、计费、多用户） | 我们是单机单用户的号池，**不做**多用户/计费/渠道分销（见路线图"有意不做"） |

一句话：**opencode-pool = "额度窗口语义"的号池**——别的项目管"把请求送出去"，
我们管"送出去之前先挑一个还没死的号，且同一对话别换号"。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/quickstart.md](docs/quickstart.md) | 5 分钟跑通（含 DSH 插件装法） |
| [docs/configuration.md](docs/configuration.md) | 配置逐字段参考（providers / accounts / sticky / 水位线…） |
| [docs/adapters.md](docs/adapters.md) | Adapter 契约 + `adapters/ext/` 掉落目录 + 最小示例 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | 报错判据（502 / 503 / 日志 / 指标含义） |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 分层、扩展点、踩过的坑 |
| [docs/verification-20261106.md](docs/verification-20261106.md) | v0.2.0 端到端验收记录（测试 / 运行态 / 文档一致性抽查） |
| [docs/analytics.md](docs/analytics.md) | 结构化请求日志分析：`stats.py` 用法与每个数怎么读 |
| [deploy/README.md](deploy/README.md) | Linux systemd / Windows 任务计划程序 / Docker |
| [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) | 贡献流程 · 威胁模型与漏洞报告 |
| [ROADMAP.md](ROADMAP.md) | 0.1 → 0.2 → 0.3 形态演进与想法池 |
| [CHANGELOG.md](CHANGELOG.md) | 变更记录 |

## 部署形态

- **DSH 插件**（`dsh-plugin/`）：侧边栏面板，宿主侧代理 + token 注入，局域网可用；
- **独立服务**：`opencode-pool serve`，裸跑 / `scripts/start.sh` / systemd（Linux）/
  任务计划程序（Windows）/ Docker（`docker compose up -d`）。

## 安全边界

- 监听默认 `127.0.0.1`；面板只对 loopback 提供，token 由服务端注入页面；
- 密钥落 `~/.dsh/opencode-pool/secrets.json`（目录 `0700` / 文件 `0600`），
  API 只回"来源 + 8 位指纹"；
- 面板写配置前先校验、先备份（`*.bak-<时间戳>-panel`），原子写；
- **别把 8790 端口裸露到公网**。详见 [SECURITY.md](SECURITY.md)。

## 注意

OpenCode 的条款写明会监控滥用流量。多账号合并成一个入口是否被判定为滥用，由上游决定；
本项目的定位是"自己付费的多个订阅合起来少踩限流"，不是账号共享服务。

## License

[MIT](LICENSE) · Copyright (c) 2026 lugabe / GitHub LUgABeEEE
