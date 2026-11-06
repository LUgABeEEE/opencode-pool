# 5 分钟快速开始

目标：跑起中转 → 面板加第一个号 → 客户端接上。全程不需要密钥出现在任何仓库里。

## 0. 环境

- Python 3.10+（唯一运行时依赖 `requests`：`python3 -m pip install requests`）
- （可选）DSH：想要侧边栏面板时才需要

## 1. 生成配置（init）

```bash
cd /path/to/opencode-pool
python3 -m opencode_pool init
```

会在 `~/.dsh/opencode-pool/` 下生成 `pool.config.json`（`0600`），
里面带一个随机 `relay_token`（客户端调用时要带它）。已有配置不覆盖，
要重来加 `--force`。

## 2. 起服务（serve）

```bash
python3 -m opencode_pool serve
# [pool] listening http://127.0.0.1:8790  providers=['opencode-go']  accounts=[]
```

默认监听 `127.0.0.1:8790`。想后台跑：`./scripts/start.sh start`
（或 systemd / 任务计划程序，见 [../deploy/README.md](../deploy/README.md)）。

## 3. 面板加号

浏览器打开 <http://127.0.0.1:8790/> —— 就是控制台（面板只对 loopback 开放）。

1. 点「加号」，填：账号名（字母/数字/`_ . -`）、上游（默认 `opencode-go`）、API key；
2. 提交后密钥存进 `~/.dsh/opencode-pool/secrets.json`（`0600`），
   面板只显示 8 位指纹，**不再回显明文**；
3. 右侧窗口卡（5 小时 / 周 / 月）显示三档额度 —— 数据来自上游 `/v1/usage`，
   每 `usage_refresh_seconds`（默认 90 秒）刷新一次。

多来几个号，池子就开始按**余量**选号了：哪号快打满就躲开，打满的自动进冷却。

## 4. 客户端接上

把客户端指向中转，`base_url` 用 `http://127.0.0.1:8790/v1`，API key 用 `relay_token`
（`init` 时写在配置里的那串；忘了可以打开 `pool.config.json` 查看）：

```bash
curl http://127.0.0.1:8790/v1/chat/completions \
  -H "Authorization: Bearer <relay_token>" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"hi"}]}'
```

跑通上面这条 curl 就说明链路 OK。OpenAI SDK / Claude Code / Codex 都指过来即可
（Anthropic 走 `/v1/messages`，Responses 走 `/v1/responses`）。

## 5.（可选）DSH 插件：侧边栏面板

装进 DSH profile 后，侧边栏出现「OpenCode 号池」，面板经 dsh-web 代理，
**局域网访问也能用**（不会踩到"浏览器里的 127.0.0.1 是用户自己电脑"的坑），
relay token 由宿主侧注入、浏览器拿不到：

```bash
dsh plugin add /path/to/opencode-pool/dsh-plugin
```

插件默认会自己拉起 `python3 -m opencode_pool serve`（可在插件配置里关掉
`autostart`），卸载时收掉子进程。细节见 [../dsh-plugin/README.md](../dsh-plugin/README.md)。

> 装完刷新 dsh-web 页面；面板没出现先看
> [troubleshooting.md](troubleshooting.md) 的「DSH boot 卡在 Failed to load plugins」。

## 常用命令

```bash
python3 -m opencode_pool check    # 校验配置（改完配置先跑这个）
python3 -m opencode_pool status   # 看池子：在线几个号、余量、冷却原因
./scripts/start.sh start|stop|status   # 无 systemd 时的托管
```

## 下一步

- 逐字段配置参考：[configuration.md](configuration.md)
- 加新号源 / 写适配器：[adapters.md](adapters.md)
- 出问题先查：[troubleshooting.md](troubleshooting.md)
- 开机自启：[../deploy/README.md](../deploy/README.md)
