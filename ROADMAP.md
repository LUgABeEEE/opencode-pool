# 路线图

## 0.1 — DSH 插件形态（当前）

- [x] OpenCode Go 适配器：三档额度窗口、`x-opencode-session`、429 按窗口冷却
- [x] 池调度：headroom 选号、失败换号、冷却持久化
- [x] 控制台：窗口卡 / 三环仪表 / 寿命带 / 加号·换 key·删号
- [x] 适配器注册表 + `adapters/ext/` 掉落目录（加号源不改核心）
- [x] DSH 插件：侧边栏面板 + 宿主侧反向代理 + 中转进程托管
- [x] 23 个回归用例

**0.1 有意不做**：多用户、计费、跨机共享状态、Windows 服务化 —— 先把单机单用的链路做扎实。

## 0.2 — 脱离 DSH 也能用（其他 agent）

目标：Claude Code / Codex / 任意 OpenAI SDK 都能直接指过来，不再依赖 dsh-web 代理。

- [ ] `opencode-pool serve` 独立对外（可选 0.0.0.0 + 自己的鉴权中间件）
- [ ] 客户端档案（profiles）：给 Claude Code / Codex / 通用 SDK 各出一段"怎么接"的生成器
- [ ] 模型映射表：把不同 agent 眼里的模型名对到池内模型
- [ ] 额度历史与图表（现在只有采样点，没有前端曲线）
- [ ] systemd unit / launchd plist 模板 + `--install-service`

## 0.3 — 跨平台发行形态

目标：Linux / Windows / macOS 上"下载即用"，不再要求 Python 环境。

- [ ] 单文件二进制（PyInstaller / Nuitka）或 Go 重写；面板内嵌
- [ ] Windows：服务化（计划任务 / nssm 包装）+ 无管理员权限的便携模式
- [ ] Linux：deb / tar.gz + systemd unit；`update` 子命令
- [ ] macOS：launchd + Homebrew formula（视需求）
- [ ] 插件市场发布（DSH 侧）、版本升级与配置迁移（schemaVersion 已在配置里预留）

## 想法池（未排期）

- 按模型路由到不同上游（已有"显式声明优先"的雏形）
- 每个账号的并发上限（`max_inflight`）
- 一个账号挂多把 key 做轮换（当前是"一把 key 一个账号"）
- 额度低水位时主动告警（接 DSH 的夜间巡检 / 微信/QQ 通道）
