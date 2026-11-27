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

## 未覆盖

- Windows 真机上的托盘图标、注册表写入（`HKCU\Environment`）、PyInstaller 打包：
  本机是 Linux，只有代码级校验与自检里的逻辑用例。发布前建议在 Windows 上跑一次
  `python winclient/selftest.py` + `pwsh -File winclient/build_exe.ps1`。
- 上游真实账号的额度/限流行为：自检用假 key，只验证"401 不影响进程"。
