# 部署：systemd（Linux）与任务计划程序（Windows）

中转本体是纯 Python（除 `requests` 无第三方依赖），两种系统都能裸跑；
下面给"开机自启 + 挂了自动拉起"的托管方式。

前置（两种系统通用）：

```bash
# 1. 装依赖（唯一运行时依赖 requests）
python3 -m pip install requests      # Windows: py -m pip install requests

# 2. 生成配置（默认 ~/.dsh/opencode-pool/pool.config.json，含随机 relay token）
python3 -m opencode_pool init

# 3. 手动跑一次确认没问题（Ctrl-C 停）
python3 -m opencode_pool serve
# 判存活看端口/进程；/healthz 是就绪语义 —— 刚 init 完还没加账号时回 503 属正常
curl -s http://127.0.0.1:8790/healthz
```

确认能跑之后再做下面的服务化。

---

## Linux：systemd

### 1. 放好代码与 unit

```bash
# 假设检出到 /opt/opencode-pool（换位置就同步改 unit 里的 WorkingDirectory）
sudo mkdir -p /opt/opencode-pool
sudo cp -r . /opt/opencode-pool/

sudo cp /opt/opencode-pool/deploy/opencode-pool.service /etc/systemd/system/
sudo systemctl daemon-reload
```

unit 里三处路径要按实际改（默认写的是这台机器的常见布局）：

| 项 | 默认值 | 说明 |
| --- | --- | --- |
| `User` / `Group` | `agent` | 运行身份；密钥仓属主要和它一致 |
| `WorkingDirectory` | `/opt/opencode-pool` | 仓库检出位置（`pip install .` 过可省） |
| `Environment=OPENCODE_POOL_CONFIG` | 默认不设置 | 不设就用服务用户的 `~/.dsh/opencode-pool/pool.config.json`；要自定义必须写**绝对路径**（systemd 不展开 `~`） |

### 2. 启动与自启

```bash
sudo systemctl enable --now opencode-pool
systemctl status opencode-pool        # 判存活：active (running) 即成功
curl -s http://127.0.0.1:8790/healthz # 判可用性：200 = 有可调度账号（503 = 起来了但没号）
journalctl -u opencode-pool -f        # 看 [req] / [pool] 日志
```

`Restart=always` + `RestartSec=3`：进程挂了 3 秒后自动拉起。

### 3. 卸载

```bash
sudo systemctl disable --now opencode-pool
sudo rm /etc/systemd/system/opencode-pool.service
sudo systemctl daemon-reload
```

### 没有 root / 不想用 systemd 时

仓库自带 `scripts/start.sh`（`setsid nohup` 托管）：

```bash
./scripts/start.sh start    # 起（写 ~/.dsh/opencode-pool/relay.pid 与 relay.log）
./scripts/start.sh status   # 看池子
./scripts/start.sh stop     # 停
```

---

## Windows：任务计划程序

Windows 上没有 systemd。用「任务计划程序」做"开机自启 + 失败重启"，
比手写命令直观，步骤如下（Windows 10 / 11 通用）。

### 图形界面步骤

1. **打开任务计划程序**：`Win + R` → 输入 `taskschd.msc` → 回车。
2. **创建任务**（注意用「创建任务」，不是「创建基本任务」，才能看到全部选项卡）：
   右侧点「创建任务…」。
3. **「常规」选项卡**：
   - 名称：`opencode-pool`；
   - 描述随意，例如 `OpenCode 号池中转`；
   - 选择「不管用户是否登录都要运行」（想让面板一直在，就选这个）；
   - **不要**勾「使用最高权限」——服务不需要管理员。
4. **「触发器」选项卡** → 「新建…」：
   - 开始条件选「启动时」（开机就起，不用登录）；
     如果只想登录后起，选「登录时」；
   - 勾选「已启用」→ 确定。
5. **「操作」选项卡** → 「新建…」：
   - 操作：「启动程序」；
   - 程序或脚本：填 Python 的完整路径，例如
     `C:\Users\<你>\AppData\Local\Programs\Python\Python312\pythonw.exe`
     （用 `pythonw.exe` 不弹黑框；不知道路径就在终端跑 `where python` 查）；
   - 添加参数：
     ```
     -m opencode_pool serve --config "%USERPROFILE%\.dsh\opencode-pool\pool.config.json"
     ```
   - 起始于（重要）：填仓库目录，例如 `C:\Users\<你>\opencode-pool`
     ——不填的话 `python -m opencode_pool` 找不到包，任务会秒退。
6. **「设置」选项卡**：
   - 勾「如果任务失败，按以下频率重新启动：1 分钟，重试 3 次」；
   - 勾「如果请求后任务仍在运行，将其强行停止」（更新代码后重新触发任务时用）；
   - 「停止任务如果超过：」可以设 0（不限制，中转本来就是长跑进程）。
7. 点「确定」。如果第 3 步选了「不管用户是否登录」，会弹窗要密码 —— 输入当前用户的登录密码。
8. **验证**：任务列表里右键 `opencode-pool` → 「运行」，然后
   `curl http://127.0.0.1:8790/healthz`（或浏览器打开 <http://127.0.0.1:8790/>）。

停止 / 卸载：右键任务 → 「结束」；不再需要就「删除」。

### 命令行等价做法（可选）

喜欢命令的话，下面一条等价于上面 2–7 步（在 **cmd** 里跑；路径按实际替换）：

```bat
schtasks /Create /TN "opencode-pool" /TR "C:\Users\<你>\AppData\Local\Programs\Python\Python312\pythonw.exe -m opencode_pool serve --config %USERPROFILE%\.dsh\opencode-pool\pool.config.json" /SC ONSTART /RU <你> /RL LIMITED /F
```

注意 `schtasks` 没法设「起始于」工作目录 —— 所以**推荐用图形界面**；
或者把 `pyproject.toml` 装一遍（`py -m pip install .`），让包进 site-packages，
就不依赖工作目录了。

### Windows 上的路径注意

- 配置里 `state_file` / `pid_file` / `secrets_file` 用相对路径时，
  按配置文件所在目录解析（写 `pool_state.json` 就落在 `~/.dsh/opencode-pool/`）；
- 密钥仓 `secrets.json` 在 `%USERPROFILE%\.dsh\opencode-pool\`，
  Windows 上靠 NTFS 权限保护（右键属性 → 安全，只留自己）。

---

## Docker（补充）

Linux / Windows（Docker Desktop）都能用镜像跑，见仓库根的
[Dockerfile](../Dockerfile) 与 [docker-compose.yml](../docker-compose.yml)：

```bash
docker compose up -d
```

两个坑（详见 SECURITY.md 与 compose 注释）：

1. 容器内配置的 `listen` 要写 `0.0.0.0:8790`（默认 `127.0.0.1` 在容器里出不去）；
2. `secrets_file` 指到 `/data/secrets.json`，否则密钥随容器销毁丢失。

## 装完自检清单

- [ ] `curl http://127.0.0.1:8790/healthz` 返回 200（就绪 = 有可调度账号；
      503 = 进程活着但没号，用 `status` 看原因，不代表服务坏了）
- [ ] `python3 -m opencode_pool status` 能打印各账号余量
- [ ] 重启机器后服务自动回来
- [ ] 面板 <http://127.0.0.1:8790/> 能加号、换 key
- [ ] `journalctl -u opencode-pool`（Linux）/ 任务历史（Windows）里没有秒退记录
