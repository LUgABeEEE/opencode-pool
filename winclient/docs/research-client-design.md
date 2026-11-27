# 同类「号池客户端」都是怎么做的 —— 调研与取舍（2026-11-06）

调研目的：给 opencode-pool 做 Windows 客户端之前，先看别人已经踩过的路，
避免把"外壳"做成重复的面板，或者漏掉成熟项目当成常识的几件事。

抓取方式：GitHub API 检索同赛道项目 + 直接读各仓库 README（非转述）。
下面每条都能对回原始 README。

---

## 1. 调研对象

| 项目 | 形态 | 一句话 |
| --- | --- | --- |
| [170-carry/codex-tools](https://github.com/170-carry/codex-tools) | 桌面 GUI + CLI/TUI + 本地 `/v1` | Codex 多账号：导入、用量、切换登录态、反代、cloudflared |
| [lilong7676/codex-pool](https://github.com/lilong7676/codex-pool) | 纯 CLI | 同上但明确"no GUI, tray, or cloudflared"——是 codex-tools 的 CLI 减法版 |
| [farion1231/cc-switch](https://github.com/farion1231/cc-switch) | Tauri 2 桌面 | 多 AI CLI 的 provider 切换 + 本地路由 + failover + 用量统计 + MCP/Skills |
| [donyst/kiro-cli-pool-proxy](https://github.com/donyst/kiro-cli-pool-proxy) | 单二进制 + `//go:embed` 的 `/admin` | Kiro 账号池反代，React+Tailwind，SSE 实时流，按 key 计量 |
| [Hxjcc/cline-pass-switcher-go](https://github.com/Hxjcc/cline-pass-switcher-go) | 单二进制 + 内嵌前端 | Cline Pass 池：协议转换、会话粘性、分发密钥、请求历史与 Token 统计 |
| [tbphp/gpt-load](https://github.com/tbphp/gpt-load) | Docker + Web | 通用 key 池：**凭证 → 组 → AccessKey** 三层 |
| [router-for-me/CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) | 内核 + 一大票外壳 | 见下方"生态"——同一内核长出托盘、菜单栏、Web 面板三种外壳 |
| [kitephp/CLIProxyAPI_Tray](https://github.com/kitephp/CLIProxyAPI_Tray) | PowerShell 托盘脚本 | 单实例常驻、启停重启、托盘双击行为可配、自动更新 |
| [nguyenphutrong/quotio](https://github.com/nguyenphutrong/quotio) | macOS 菜单栏 + CLI | "quota at a glance"：账号、重置窗口、内核生命周期、自动更新 |
| [super-mortal/opencode2api](https://github.com/super-mortal/opencode2api) | 单二进制 + Web 面板 | 同赛道对手：模型别名、reasoning 映射、对外密钥、代理测速 |

CLIProxyAPI 的 README 里挂了一整排第三方外壳（Quotio / ProxyPilot / 霖君 /
CPA Dashboard / Oh-My-CPA / CLIProxyAPI Tray / Quota Inspector…），
说明这类项目的**外壳是被生态反复重写的部分**——内核稳定、外壳随人换。

## 2. 形态矩阵

| 维度 | 只有 Web 面板 | 托盘 / 菜单栏 | 桌面 GUI | 单二进制自带面板 |
| --- | --- | --- | --- | --- |
| 代表 | gpt-load、opencode2api | CLIProxyAPI_Tray、Quotio | cc-switch、codex-tools | KiroPool、Cline Pass Switcher |
| 强项 | 增删改查齐全、跨机可看 | 常驻、一眼看额度、启停省事 | 接驳一键切换、配置可视化、托盘+面板合一 | 部署最省事（一个文件） |
| 弱项 | 要开浏览器、不知道服务死没死 | 复杂表单难做 | 打包体积/自动更新成本 | 前端要 embed，改界面得重编 |

**共同点：没有一个项目把"池子"放进客户端里。** 客户端一律是
「本机 loopback 内核 + 外壳」。opencode-pool 已经有内核（`serve`），
所以我们的活只有外壳这一半。

## 3. 七个被反复实现的功能（成熟项目的"常识"）

1. **单实例 + 托盘常驻**
   `CLIProxyAPI_Tray`：「托盘常驻，单实例运行」，并把"托盘双击行为"做成可配；
   `cc-switch` 托盘可切 provider；`codex-tools`「状态栏驻留」+ Windows 任务栏额度。
   → 客户端第二次启动应当是**唤出已有窗口**，而不是再起一个内核抢端口。

2. **内核生命周期可视化**
   启动 / 停止 / 重启 / 是否活着 / 监听地址，全部在界面上，而不是让用户
   去翻 `ps` 或日志。`Quotio`：「CLIProxyAPI lifecycle」是菜单栏里的第一等公民。

3. **账号状态带时间语义**
   不写"余额 20%"，写"5h 窗口 82%，16:40 重置"；打满的号要能解释"为什么不用它"。
   `codex-tools` 展示 5h / 1week 窗口 + 重置时间 + 计划类型；`Quotio`
   「Track account limits and reset windows」；我们上游本来就有 `resets_at`。

4. **一键接驳 + 一键恢复**
   `codex-tools`：「一键把 Codex App/CLI 切到本机反代地址，也可一键恢复原配置」；
   `cc-switch`：「Switching replaces only the connection details…自动备份每个配置文件」。
   → 客户端最值钱的动作不是"看"，是**把 base_url/key 填进别的工具再能还原**。

5. **写配置前预览 + 备份**
   `cc-switch`：「The editor shows what the config file will look like after switching,
   and CC Switch automatically backs up each config file before rewriting it」。
   → 我们所有写盘动作（加号/换 key/改参数/接驳）都要留 `.bak-<时间戳>`。

6. **请求历史 + 用量统计独立成页**
   `Cline Pass Switcher`：最近 500 条、列可配/可拖拽排序、点行开详情抽屉、
   按日/模型/账号/密钥统计、365 天汇总；`CPA Dashboard`：实时日志流 +
   SQLite 记录 + 用量分析。→ opencode-pool 已有 `requests.jsonl`（21 字段），
   客户端只差"读它、聚合它、能筛"。

7. **危险操作二次确认，密钥永不出屏**
   `gpt-load` 的 AccessKey 只显示可用组与协议；`opencode-pool` 面板只回
   "来源 + 8 位指纹"。→ 客户端沿用：只显示指纹，复制 token 要点一下才明文。

## 4. 反例（明确不学的部分）

| 做法 | 为什么不要 |
| --- | --- |
| 在客户端里做协议转换 | 上游 `server.py` 已经转了 OpenAI/Anthropic/Responses 三套，重复实现只会漂 |
| 多用户 / 计费 / 分销 | opencode-pool 的 ROADMAP 明确"有意不做"，客户端跟着不做 |
| Electron / Tauri 重打包 | 本机只要 Python + tkinter 就能跑；打包成本与自动更新是纯负债，除非要做分发 |
| 把 Web 面板塞进 WebView 当客户端 | 面板是 loopback-only 且已经存在；客户端的增量价值在托盘、接驳、统计、单实例 |

## 5. 我们的设计（照什么、不照什么）

| 需求 | 参照 | 本客户端的落法 |
| --- | --- | --- |
| 单实例 | CLIProxyAPI_Tray | 命名互斥量 + 按窗口标题唤出已有实例 |
| 托盘常驻 | Quotio / CLIProxyAPI_Tray | ctypes 托盘（零依赖），菜单带"可用 n/m"，双击唤出 |
| 内核托管 | Quotio lifecycle | 子进程 + 日志管道 + 健康探测；外部已跑的实例只 attach 不杀 |
| 三档窗口可视化 | codex-tools | 三个额度环 + 重置倒计时 + "池内最坏点"聚合 |
| 状态可解释 | cc-switch 健康监控 | 状态胶囊按 readiness 同序判据：暂停→缺钥→冷却→打满→低水位→采样过期 |
| 接驳向导 | codex-tools / cc-switch | base_url + token + 五种客户端片段 + 真发一次 ping 自检 |
| 一键写入/恢复 | codex-tools | 写 Codex `config.toml` / 用户环境变量，写前备份，一键还原 |
| 请求历史 | Cline Pass Switcher | 读 `requests.jsonl` 尾部，列筛选 + 详情 + 按账号/模型/结果聚合 |
| 写盘安全 | cc-switch | 全部走 `localcfg`：先备份、原子替换、0600 |
| 密钥不显 | gpt-load / 面板 | 默认掩码，点眼睛才显示；只显示 8 位指纹 |

## 6. 本次调研直接改掉的三件事

1. **加单实例**（原设计漏了）：否则双击两次 = 两个内核抢 8790。
2. **接驳页从"只给片段"升级为"能写入 + 能还原"**：这才是客户端相对面板的增量。
3. **托盘 tooltip / 菜单带池子摘要**：从"能启停"变成"一眼知道要不要动手"。

---

原始材料：本文件所有引文均来自上述仓库的 README（`raw.githubusercontent.com`，
2026-11-06 抓取）。star 数与描述可在 GitHub API
`search/repositories?q=...` 复现。
