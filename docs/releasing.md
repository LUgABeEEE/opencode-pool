# 发版（GitHub Actions → npm）

一条命令发布：打 tag 推上去，剩下交给 `.github/workflows/release.yml`。
不需要在本机 `npm login`（npm 的 web 登录会回落到交互式用户名 / 密码 / OTP，
无人值守时走不通）。

## 一次性准备：NPM_TOKEN

1. 打开 <https://www.npmjs.com/settings/~/tokens> → **Generate New Token** →
   选 **Automation**（不是 Publish：Automation 绕过 OTP，CI 才能用）
2. 复制 token（只显示一次）
3. 打开 <https://github.com/LUgABeEEE/opencode-pool/settings/secrets/actions> →
   **New repository secret** → Name 填 `NPM_TOKEN`，Value 粘贴 token → Add secret

> 用 `gh` 也行，而且不必把 token 贴进任何对话：
> ```bash
> gh secret set NPM_TOKEN --repo LUgABeEEE/opencode-pool   # 交互式粘贴，输入不回显
> ```

## 每次发版

1. 改版本号（三处必须一致，workflow 会校验）：

   | 文件 | 字段 |
   | --- | --- |
   | `dsh-plugin/package.json` | `version` |
   | `pyproject.toml` | `version` |
   | `opencode_pool/__init__.py` | `__version__`（不强制，但别落下） |

2. 更新 `CHANGELOG.md`，走 PR 合进 `main`（`main` 有分支保护：5 个必需检查 + PR）
3. 打 tag 并推送：

   ```bash
   git checkout main && git pull
   git tag v0.2.3
   git push origin v0.2.3
   ```

4. Actions 里等 `release` 跑完：version 校验 → 回归 → `npm pack --dry-run` →
   `npm publish --provenance` → 建 GitHub Release。

## 预演与重跑

- 预演：Actions → "release" → **Run workflow** → 勾 `dry_run`（只打包，不发布）
- 重跑安全：同版本已存在时 `npm view` 会命中，workflow 记一条 notice 并跳过，
  不会红；Release 已存在也跳过

## 失败怎么判

| 报错 | 原因 |
| --- | --- |
| `缺少 NPM_TOKEN secret` | 没配或名字拼错（必须是 `NPM_TOKEN`） |
| `tag x 与 dsh-plugin/package.json y 不一致` | 版本号没同步就打了 tag |
| `EOTP` / `one-time password` | 用了 Publish token 而不是 Automation token |
| `403 Forbidden — you do not have permission` | token 属于别的账号，或该包名已被别人占用 |
| `provenance` 相关报错 | 仓库必须 public，且 workflow 要 `id-token: write`（本文件已给） |

## 撤销

- 发布后 72 小时内：`npm unpublish dsh-plugin-opencode-pool@x.y.z --force`（需要 token 与权限）
- 超过 72 小时：只能发新版本修（`0.0.0` 之类占位也可以 `npm deprecate` 标记）
