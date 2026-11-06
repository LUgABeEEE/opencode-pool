# dsh-plugin-opencode-pool

把 [opencode-pool](../README.md) 装进 DSH：侧边栏多一个「OpenCode 号池」，面板经 dsh-web 代理。

## 装

```bash
dsh plugin add /path/to/opencode-pool/dsh-plugin
```

`dsh plugin add` 读 `package.json → dsh.bundle.patch`，把包加进 profile 的 `dsh.profile.bundles`，
启动时合并本插件的 `cordis.patch.yml`（只贡献一行 Loader 配置）。

> 不要在 profile 的 `cordis.patch.yml` 里手抄这段 `insert` —— id 重复会让设置页保存变歧义。
> 要改配置只写顶层行：`- id: opencode-pool` + `config: {...}`（整体替换，不是深合并）。

## 它做什么

| 半侧 | 职责 |
| --- | --- |
| 宿主侧 `lib/index.js` | ① 按需拉起 `python3 -m opencode_pool serve`（卸载时 SIGTERM 收掉）② 把 `/api/opencode-pool/*` 反代到中转 ③ **代理时注入 relay token**，浏览器拿不到 |
| 客户端 `lib/client.js` | 侧边栏条目 + 主面板（iframe 指向 `api/opencode-pool/panel`） |

为什么不用 iframe 直接连 `127.0.0.1:8790`：面板可能从局域网打开
（`http://192.168.1.11:3080`），那时浏览器里的 127.0.0.1 是**用户自己的电脑**。
经 dsh-web 转一次，局域网也能用；顺带把 token 留在服务端。

面板本身仍是 `opencode_pool/panel/panel.html` 那一份，两个入口共用：
直连中转时挂 `/`，经插件时挂 `/api/opencode-pool/panel`（面板按 `location.pathname` 反推 API 前缀）。

## 三个 id 别搞混（踩过一次，会让整页 boot 卡死）

| 名字 | 值 | 写在哪 |
| --- | --- | --- |
| **npm 包名 ＝ 客户端模块 id** | `dsh-plugin-opencode-pool` | `package.json.name` **和** `lib/client.js` 里 `__ModuleLoader__.load({ id })` —— 两者必须一致 |
| cordis 条目 id | `opencode-pool` | 本插件 `cordis.patch.yml` 的 `insert[].id` |
| 侧栏 slot id | `opencode-pool` | `lib/client.js` 的 `SLOT_ID`（sidebar id 与 `main` 槽位的 key 必须相同） |

**`__ModuleLoader__.load({ id })` 的 id 是包名，不是条目 id。** 写成条目标识时，
loader 执行完文件却在注册表里找不回这个包，于是报：

```
import failed: client-modules: could not load "dsh-plugin-opencode-pool" without registering
```

而 DSH 的 web boot 是"全有全无"——任何一个客户端条目没起来，整页就停在
`Failed to load plugins`，且每次点名的插件可能不同。测试 `tests/plugin.test.mjs`
里有一条断言专门守这个等式，改坏会红。

### 报错怎么读

| 报错形态 | 含义 | 翻哪个文件 |
| --- | --- | --- |
| `… could not load "X" without registering` / `already executed without registering` | 模块 id 不对，或文件没包 `__ModuleLoader__.load` | `lib/client.js` 第一行的 `id` |
| `X: failed`（没有额外说明） | 插件 `apply()` 期间抛错 | 该插件的 `apply()` / 宿主侧入口 |

## 配置

```yaml
- id: opencode-pool
  config:
    host: 127.0.0.1
    port: 8790
    projectDir: ''          # 留空 = 环境变量 OPENCODE_POOL_HOME 或 ~/.dsh/opencode-pool
    python: python3
    autostart: true
    configPath: ~/.dsh/opencode-pool/pool.config.json
    logPath: ~/.dsh/opencode-pool/relay.log
```

`projectDir` 必须指向**含 `opencode_pool/` 包的目录**（也就是本仓库根）。中转的日志写在
`logPath`，进程退出码也会打到 DSH 日志里，方便排"为什么面板 502"。

## 路由

| 路径 | 说明 |
| --- | --- |
| `GET /api/opencode-pool` | 插件索引：版本、中转地址、健康状态、可用路由 |
| `GET /api/opencode-pool/panel` | 控制台 HTML（中转注入 token 后再代理） |
| `* /api/opencode-pool/v1/...` | 中转全部 API 与 OpenAI 兼容端点 |

## 中转进程的归属（重启时会看到它"没了"）

插件拉起的 python 进程和 dsh-web **同 cgroup**（`systemctl show -p KillMode dsh-web` 是
`control-group`），所以 dsh-web 一重启，中转跟着被收掉 —— 这是预期行为，不是泄漏：

- 下次 boot 时插件会重新拉起（`autostart: true`）
- 手动 `setsid` 起的中转也逃不掉（cgroup 不认进程组），别指望它跨重启

**推论**：如果重启后 8790 连不上，先看 `~/.dsh/opencode-pool/relay.log` 有没有新的一行
`unrecognized arguments` / Traceback —— 那说明拉起失败，而不是"进程没被叫醒"。

## 排障

- **面板 502**：中转没起来。看 `~/.dsh/opencode-pool/relay.log`；或先手动
  `python3 -m opencode_pool serve` 确认能起
- **`projectDir` 报错**：插件找不到 `opencode_pool/__main__.py`，把 `projectDir` 指对
- **局域网 403/401**：这是 dsh-web 自身的鉴权在拦，和插件无关

## 安全

面板挂在 dsh-web 上，鉴权沿用 dsh-web 自身登录态 —— 但**不要把 dsh-web 裸露到公网**。
写入类路由（加号/换 key/删号）只有登录用户能点到；中转 token 由宿主侧注入，不落浏览器。
