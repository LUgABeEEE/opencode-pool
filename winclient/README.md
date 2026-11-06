# opencode-pool · Windows 客户端

给 [opencode-pool](../README.md) 做的桌面外壳。**内核一行没改**，
客户端只负责把它托管起来、把"哪个号现在能用、还能用多久"变成一眼能看懂的东西、
以及把中转接进别的工具之后还能一键还原。

```
┌───────────────────────────── 客户端（tkinter，零第三方依赖） ─────────────────────────────┐
│  托盘 / 单实例 / 开机自启                                                                  │
│  仪表盘      三档额度环 · 冷却倒计时 · 池内最坏点                                          │
│  账号        加号 / 换 key / 暂停 / 删除 / 清冷却（服务没起来时也能改配置）                 │
│  接驳        base_url+token · 五种片段 · 一键写 Codex config.toml / 用户环境变量 · 一键还原  │
│  请求        requests.jsonl 筛选聚合 + relay.log 实时滚动                                  │
│  设置        客户端偏好 / 中转参数 / 诊断（check、打开目录、重启）                          │
└───────────────────────────────────┬────────────────────────────────────────────────────┘
                                    │ 子进程 + 日志管道 + /healthz 探测
                        python -m opencode_pool serve   ←  上游内核，没动
```

## 快速开始

```powershell
cd opencode-pool\winclient
python -m pip install requests      # 内核唯一依赖
```

然后任选一种：

```powershell
.\run_client.cmd                    # 双击这个也行（无控制台窗口）
python run_client.py                # 带控制台，方便看报错
python -m opencodepool_client       # 等价
```

首次启动会自动生成 `~/.dsh/opencode-pool/pool.config.json`（含随机 `relay_token`），
然后把界面停在「账号」页：**池子里没有号时内核起不来**（上游 `validate()` 直接退出），
所以第一步一定是"加一个号"。

加完号会自动起中转；左下角状态灯变绿、仪表盘出现三档额度环就通了。

## 五个页面各管什么

| 页面 | 你会在这里做的事 |
| --- | --- |
| 仪表盘 | 看池子：可用 n/m、近 5 分钟请求数、三档窗口的"池内最坏点"、每个号的余量与回血倒计时 |
| 账号 | 加号 / 换 key / 暂停 / 删除 / 查额度 / 清冷却 / 热重载；服务没起来时改的是配置文件 |
| 接驳 | 复制 `base_url` 与 token；五种客户端片段；**一键写 Codex 配置 / 环境变量并能还原**；真发一次 ping 自检 |
| 请求 | `requests.jsonl` 按账号/结果筛选、看 P50/P95 与缓存命中；切到「控制台日志」看内核输出 |
| 设置 | 项目目录 / Python / 刷新间隔 / 托盘与自启；中转参数（水位线、并发、粘性、采样周期）；`check` 诊断 |

## 为什么是 tkinter

- 内核已经是 Python，外壳跟着用同一个解释器，**不用装 Node / Rust / WebView2**；
- 托盘用 ctypes 直接调 `Shell_NotifyIconW`，图标用 zlib 手写 ICO，全在标准库内；
- 想发单文件 exe 时 `build_exe.ps1` 一条命令，不需要前端构建链。

代价是界面不如 Tauri 精致。这个取舍和本项目的定位一致：**外壳只做观测与控制**，
真正的复杂度在 `pool.py` 的调度里。

## 会改哪些文件

| 文件 | 谁写 | 说明 |
| --- | --- | --- |
| `~/.dsh/opencode-pool/pool.config.json` | 加号/换 key/改参数 | 每次写盘前留 `.bak-<时间戳>-client` |
| `~/.dsh/opencode-pool/secrets.json` | 加号/换 key | `{"version":1,"secrets":{slot:{value,updated_at}}}`，0600 |
| `~/.dsh/opencode-pool/relay.log` | 内核 stdout/stderr | 客户端实时 tail，退出后仍在 |
| `~/.dsh/opencode-pool/requests.jsonl` | 内核 | 客户端只读 |
| `%APPDATA%\opencode-pool-client\client.json` | 设置页 | 客户端自己的偏好 |
| `~/.codex/config.toml` | 「接驳 → 一键写入」 | 只改 `model_provider` 与 `[model_providers.opencode-pool]`，写前存 `.opencode-pool-backup` |
| `HKCU\Environment` | 「接驳 → 环境变量」 | 原值存 `%APPDATA%\opencode-pool-client\env-backup.json`，可还原 |
| `HKCU\...\Run\OpenCodePoolClient` | 设置页 → 开机自启 | 带 `--minimized`，开机只落托盘 |

## 和"外部已经在跑的中转"共存

客户端启动时先探 `/healthz`：

- 探到在跑 → **attach 模式**（状态灯显示"外部实例"），停止按钮不会去杀别人拉起的进程；
- 没在跑 → 自己拉一个 `python -m opencode_pool serve`，退出时按设置决定要不要停。

这样 DSH 插件、`start.sh`、任务计划程序三种来源的中转都能被同一个客户端观察。

## 自检

```powershell
python selftest.py          # 45 项：临时目录造配置 → 真起中转 → 加号/查额度/停服 → 日志解析 → 托盘/单实例
python selftest.py --keep   # 保留临时目录排障
```

GUI 冒烟（会自己退出）：

```powershell
python -c "import sys;sys.path.insert(0,'.');from opencodepool_client.app import App;a=App(minimized=True);a.after(2500,a.do_exit);a.mainloop()"
```

## 打包成 exe

```powershell
pwsh -File build_exe.ps1            # 目录版
pwsh -File build_exe.ps1 -OneFile   # 单文件
```

exe 里**不含内核**：仍靠 `python -m opencode_pool serve` 拉起。首次运行到「设置」确认
项目目录与 Python 路径即可（这两项存在 `client.json` 里，升级 exe 不会丢）。

## 排障

| 现象 | 先看哪里 |
| --- | --- |
| 状态灯一直灰/红 | 「请求 → 控制台日志」；多半是项目目录指错或缺 `requests` |
| 加号后额度是 `—` | key 无效或上游 401；账号行的 note 会写 `usage HTTP 401` |
| 端口被占 | 设置里换 `listen`，或先停掉别处的中转；客户端会提示"启动即退出"并给出日志尾部 |
| 双击没反应 | 单实例在起作用：它会把已有窗口叫到前台；若仍无反应，看 `%APPDATA%\opencode-pool-client\client.json` 是否存在 |
| 托盘图标不见 | Windows 把新图标收进"隐藏的图标"里了，拖出来即可；不影响主窗口 |

## 代码结构

```
winclient/
├─ run_client.cmd          # 双击入口（pythonw）
├─ run_client.py           # 脚本入口（自动修 sys.path）
├─ build_exe.ps1           # PyInstaller 打包
├─ selftest.py             # 无界面端到端自检
├─ docs/research-client-design.md   # 同类号池客户端调研与取舍
└─ opencodepool_client/
   ├─ app.py               # 主窗口、消息泵、托盘联动、服务状态机
   ├─ paths.py settings.py # 路径解析 / 客户端偏好
   ├─ relay.py             # HTTP 客户端 + 内核进程托管
   ├─ localcfg.py          # 离线改 pool.config.json + secrets.json（带备份）
   ├─ clientcfg.py         # Codex config.toml / 用户环境变量的一键写入与还原
   ├─ logtail.py           # requests.jsonl 尾部解析与聚合
   ├─ theme.py widgets.py  # 深色主题、额度环、余量条、卡片、滚动容器
   ├─ tray.py icon.py      # ctypes 托盘 / 纯 zlib 生成 ICO
   ├─ single_instance.py   # 命名互斥 + 唤出已有窗口
   ├─ autostart.py         # 开机自启（HKCU Run）
   └─ pages/               # dashboard accounts connect requests settings
```

## License

沿用上游 [MIT](../LICENSE)。
