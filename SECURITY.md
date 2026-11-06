# 安全策略

## 威胁模型（这个项目防什么、不防什么）

opencode-pool 是**单机单用户**的号池中转：它把多个自己付费的订阅账号合成一个入口，
运行在你自己的机器上。它防的是"凭据泄露"和"误暴露"，不防"本机已被攻破"。

### 资产

| 资产 | 在哪 | 保护方式 |
| --- | --- | --- |
| 上游 API 密钥 | ① 环境变量 ② `~/.dsh/opencode-pool/secrets.json` ③ DSH `.credentials.yaml`（只读回退） | 密钥仓目录 `0700`、文件 `0600`；API 只回"来源 + 8 位不可逆指纹"，**永不回显明文** |
| relay token | `pool.config.json`（`0600`） | 面板由服务端注入，浏览器拿不到；DSH 插件路径下由宿主侧代理注入 |
| 配置文件 | `~/.dsh/opencode-pool/pool.config.json` | 面板写配置前先校验、先备份（`*.bak-<时间戳>-panel`），原子写（tmp + rename） |
| prompt / 对话正文 | 只在内存里转发 | 日志只打 `[req] 账号 模型 状态 时长 字节数`，不落 prompt；会话指纹只取哈希 |

### 边界

- **面板仅 loopback**：直连中转端口时，`/` 与 `/panel` 对非 127.0.0.1 的客户端直接 403
  （`panel is loopback-only`）。要从局域网看面板，走 DSH 插件入口
  （`/api/opencode-pool/panel`），鉴权沿用 dsh-web 自身的登录。
- **中转端口默认 `127.0.0.1:8790`**，并要求 `Authorization: Bearer <relay_token>`
  （`relay_token` 为空时等于关闭鉴权 —— 生产使用请写上）。
- **不要把 8790 裸露到公网**。需要远程访问时，请自己在前面加一层有鉴权与 TLS 的反代。

### 明确不防

- 同机同用户的其他进程：本项目与你的 shell 同权限，密钥仓 `0600` 只挡"其他人"，
  不挡攻破你账号的进程；
- 上游侧的滥用判定：多账号合并是否违反上游条款由上游决定，见 README 的「注意」；
- 软件供应链：依赖只有 `requests`，安装时请用官方 PyPI / 发行版镜像。

## 密钥处理承诺（代码层面的约束）

1. 密钥只落专用目录（`~/.dsh/opencode-pool/`，`0700`）或环境变量，**不进仓库、
   不进日志、不进测试夹具**；
2. 面板与 `/v1/usage`、`/metrics`、`/healthz` 均不输出密钥明文，只输出
   `fingerprint`（SHA-256 前 8 位，不可逆）；
3. `classify()` 的 `detail` 字段只进服务端日志，不回给客户端；
4. 提交前自检：`git diff --cached | grep -iE "apiKey|password|token|PRIVATE KEY"`。

## 漏洞报告

**请不要公开开 issue 贴利用细节。** 报告渠道（按优先级）：

1. GitHub 仓库 [LUgABeEEE](https://github.com/LUgABeEEE) 的 **Security Advisories**
   （私密报告，推荐）；
2. 在仓库开 issue 只写 `[SECURITY] 一句话摘要`，细节等私下沟通；
3. 其他渠道见仓库主页联系方式。

报告时请给：影响版本、复现步骤（脱敏后）、预期影响。一般 7 天内回复；
确认后会在修复发布时公开致谢（除非你要求不署名）。

## 已知的部署注意点

- `docker-compose.yml` 默认把 8790 绑到宿主 `127.0.0.1`，这是有意的；
- 容器内运行时，请把配置的 `listen` 写成 `0.0.0.0:8790`（容器内 loopback 出不去），
  并把 `secrets_file` 指到 `/data/secrets.json`（挂载点内），否则密钥会随容器销毁丢失；
- systemd 部署建议 `UMask=0077`（unit 里已带）。
