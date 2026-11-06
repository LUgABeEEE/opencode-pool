# 贡献指南

感谢想给 opencode-pool 补东西。先记住这个项目的分工原则：

> **上游差异只进 `opencode_pool/adapters/`；调度与面板不认识任何具体上游。**

提 PR 前请把下面三件事做完。

## 1. 跑测试

项目只有两个测试入口，都不联网、不需要任何密钥：

```bash
# Python 回归（stdlib unittest，除 requests 外零依赖）
cd /path/to/opencode-pool
python3 -m compileall -q opencode_pool tests     # 语法自检
python3 -m unittest discover -s tests            # 全绿再提

# DSH 插件纯逻辑测试（node:test，零依赖）
node --test tests/plugin.test.mjs
```

改了行为就加用例：调度语义进 `tests/test_opencode_pool.py`，
插件逻辑进 `tests/plugin.test.mjs`。只跑通旧用例不算验证。

## 2. 加适配器（最常见的一种贡献）

**先看是不是零代码能解决**：任意 `/v1` 兼容上游在配置里加一段
`providers`（`adapter: openai-compatible`）就够了，不用写代码。

真的需要新适配器（特殊鉴权 / 特殊额度语义）才写代码：

1. 在 `opencode_pool/adapters/ext/` 放一个 `.py`（文件名不以 `_` 开头），
   或者做成内置适配器提交进 `opencode_pool/adapters/`；
2. 实现 `Adapter` 契约（`build_headers` / `probe` / `classify` / `extract_usage`），
   字段与额度窗口用 `Field` / `WindowSpec` 声明；
3. 契约与最小示例见 [docs/adapters.md](docs/adapters.md)；
4. 至少加三类用例：`classify()` 的失败判定、`probe()` 的窗口折算、配置校验。

写扩展时的硬约定：

- `id` 全局唯一，配置里 `providers.<pid>.adapter` 引用它；
- 失败处置写进 `classify()` 返回的 `ErrorDecision`，**不要**在核心代码里加
  `if provider == "xxx"` 分支；
- 日志与代码不得输出密钥、token 或 prompt 正文（见 [SECURITY.md](SECURITY.md)）。

## 3. 提 PR

1. 一个 PR 只做一件事（一个适配器 / 一个修复 / 一段文档）；
2. 描述里写清：动机、改了什么、测试结果（贴 `unittest` / `node --test` 的输出摘要）；
3. 涉及行为变化（配置字段、端点语义、日志格式）时同步更新对应文档：
   - 配置字段 → `docs/configuration.md`
   - 错误判据 / 日志 → `docs/troubleshooting.md`
   - 端点 → `README.md` 的端点表
   - 发布内容 → `CHANGELOG.md`
4. 不要提交密钥、真实账号、`pool_state.json`、`secrets.json`；
   `.gitignore` 已经挡了工作区外的默认路径，自检一下 `git diff --cached` 里
   没有 `apiKey` / `password` / `token` / `PRIVATE KEY` 字样的真实值；
5. 不要顺手 `git config`、不要格式化无关文件。

## 测试为什么长这样

- Python 侧刻意只用 stdlib `unittest` + `requests`：CI 与离线环境能直接跑；
- 端到端用例起一个假上游（`FakeUpstream`），验证换号与记账，不打真实上游；
- 插件测试不启动服务，只验纯逻辑（路由注册、token 读取、模块 id 一致性）——
  `client.js` 的模块 id 必须等于 npm 包名，这条断言守着一个真踩过的坑
  （见 `dsh-plugin/README.md` 的「三个 id 别搞混」）。

## 行为红线

- 不改 `~/.dsh/` 下的用户数据来做测试；测试用临时目录；
- 不重启别人的 dsh-web / 服务来做验证；
- 面板保持 loopback-only，不要把"顺便开个公网访问"塞进 PR。
