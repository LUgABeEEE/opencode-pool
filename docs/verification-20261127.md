# 0.3.0 验证记录（2026-11-27）

这次把 `winclient/`（Windows 桌面客户端）合进主线的同时修了评审发现的问题，
下面是"改完真的能跑"的证据。命令都在仓库根目录执行。

## 环境

- Python 3.12.3（Linux，无 tkinter：GUI 用例按设计 SKIP）
- Node 20.x / npm 10.9
- 依赖：`requests`

## 回归

```bash
python3 -m compileall -q opencode_pool tests scripts winclient   # OK
python3 -m ruff check opencode_pool tests scripts winclient      # All checks passed!
python3 -m unittest discover -s tests                            # Ran 123 tests ... OK
node --test tests/plugin.test.mjs                                # pass 5 / fail 0
python3 winclient/selftest.py                                    # 55 项：48 通过 / 7 跳过 / 0 失败
```

`winclient/selftest.py` 的 7 项跳过全部是平台限制：没有 tkinter（仪表盘/请求页/Tk 控件）
与没有命名互斥量（单实例）。在 Windows 上这些会实跑。

自检里实际起来的是**真子进程**：临时目录造 `pool.config.json` → 随机端口
`python -m opencode_pool serve` → `healthz`/`readiness`/`snapshot` → 在线加号/删号 →
热重载 → 停止（走 `POST /v1/pool/shutdown`）→ 确认端口释放与 `relay.log` 落盘。

## 这次的针对性用例

| 改动 | 用例 |
| --- | --- |
| Codex `config.toml` 顶层键位置 | 用户已有 `model_provider = "openai"` 且文件以 `[mcp_servers.foo]` 结尾 → 写入后 `tomllib` 解析：根上是我们、表原样保留、重复写入无重复键、还原回到接入前 |
| 请求日志协议列 | `/v1/chat/completions` → `openai`（原来显示空） |
| 进程托管 | 停止后端口释放；`relay.log` 有输出；主动停止不再触发"进程退出"回调 |
| 停止保状态 | `POST /v1/pool/shutdown` 仅 loopback；远端请求 403（`test_remote_*` 已有断言） |
| 单实例 | 非 Windows 判 SKIP（`available=False`），不再误报失败 |

## Windows 真机（2026-11-27 晚，0.3.1 的由来）

机器：elysia（Win11 23H2 + Python 3.13.9），通过 SSH 非交互会话执行，纯 CPU。

```
git clone --depth 1 --branch v0.3.0 https://github.com/LUgABeEEE/opencode-pool.git
cd winclient && python selftest.py
#   共 60 项：通过 60，跳过 0，失败 0
#   （Linux 上 55 项，多出来的 5 项是 Tk 页面渲染 / 命名互斥等平台相关用例）
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_exe.ps1
#   退出码 0，dist\OpenCodePoolClient\OpenCodePoolClient.exe = 2,278,005 B
```

**由此发现 0.3.1 修的问题**：`build_exe.ps1` 里的中文提示在 Windows PowerShell 5.1 下
（按 cp936 读 UTF-8 脚本）直接 `UnexpectedToken` 解析失败，脚本从未在 5.1 上跑通过；
`run_client.cmd` 的提示同样变乱码。两个文件已改纯 ASCII，CI 增加守门。

## 仍未覆盖

- 托盘图标在真实桌面会话里的显示效果、`HKCU\Environment` 写入与还原（会改用户环境，
  验证时故意没做，避免污染）；
- 上游真实账号的额度/限流行为：自检用假 key，只验证"401 不影响进程"。
