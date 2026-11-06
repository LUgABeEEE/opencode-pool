"""命令行入口：init / check / serve / status。

    python3 -m opencode_pool init      # 生成默认配置（~/.dsh/opencode-pool）
    python3 -m opencode_pool check     # 校验配置
    python3 -m opencode_pool serve     # 起服务
    python3 -m opencode_pool status    # 读正在跑的池子
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.request

from . import adapters
from .config import Config
from .pool import Pool
from .server import serve

HOME = os.path.expanduser("~/.dsh/opencode-pool")
DEFAULT_CONFIG = os.path.join(HOME, "pool.config.json")

TEMPLATE = {
    "version": 2,
    "listen": "127.0.0.1:8790",
    "relay_token": "",
    "credentials_yaml": os.path.expanduser("~/.dsh/.credentials.yaml"),
    "secrets_file": os.path.join(HOME, "secrets.json"),
    "state_file": "pool_state.json",
    "pid_file": "relay.pid",
    "usage_refresh_seconds": 90,
    "upstream_timeout": 900,
    "providers": {
        "opencode-go": {
            "adapter": "opencode-go",
            "base_url": "https://opencode.ai/inference/go/v1",
        }
    },
    "accounts": [],
}


def _load(path: str) -> Config:
    adapters.load_all()
    cfg = Config.load(path)
    problems = cfg.validate()
    if problems:
        print("配置有问题：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        raise SystemExit(2)
    return cfg


def cmd_init(args) -> int:
    path = args.config
    if os.path.exists(path) and not args.force:
        print(f"{path} 已存在，加 --force 覆盖", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    data = dict(TEMPLATE)
    data["relay_token"] = args.token or ("pool-" + secrets.token_urlsafe(24))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)
    print(f"已写入 {path}")
    print("下一步：python3 -m opencode_pool serve，然后打开 http://127.0.0.1:8790/ 加号")
    print(f"relay token 已生成（{len(data['relay_token'])} 字符），存在配置里，面板会自动带上")
    return 0


def cmd_check(args) -> int:
    cfg = _load(args.config)
    print(f"配置 OK · version={cfg.version} · 适配器={[a.id for a in adapters.all()]}")
    for pid, provider in cfg.providers.items():
        print(f"  provider {pid}: adapter={provider.adapter_id} base={provider.base_url or provider.adapter.base_url}")
    for account in cfg.accounts:
        print(f"  account {account['name']}: provider={account['provider']} key_env={account.get('key_env')}")
    if cfg.warnings:
        print("  提示：" + "；".join(cfg.warnings))
    return 0


def cmd_serve(args) -> int:
    cfg = _load(args.config)
    pool = Pool(cfg)
    pid_file = cfg.pid_file or os.path.join(HOME, "relay.pid")
    try:
        os.makedirs(os.path.dirname(pid_file), mode=0o700, exist_ok=True)
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    serve(pool)
    return 0


def cmd_status(args) -> int:
    cfg = Config.load(args.config)
    url = f"http://{cfg.listen}/v1/usage"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {cfg.relay_token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except Exception as exc:  # noqa: BLE001
        print(f"读不到 {url}：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"在线 {data['healthy_accounts']}/{data['total_accounts']} · 近 5 分钟 {data['requests_last_5m']} 次请求")
    for account in data["accounts"]:
        head = "—" if account["headroom"] is None else account["headroom"]
        note = account["blocked_window"] or account["cooldown_reason"] or "-"
        print(f"  {account['name']:<10} 余量 {head!s:<6} 绑定 {account['binding_window'] or '-':<8} {note}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构造解析器。

    `--config` 在**两个位置**都接受：`opencode_pool --config X serve` 和
    `opencode_pool serve --config X`。后者是插件与 start.sh 的自然写法，
    第一版只注册在顶层，于是 `serve --config X` 报 unrecognized arguments ——
    插件拉起的进程秒退，面板 502。子命令上用 SUPPRESS 默认值，避免把顶层值覆盖回默认。
    """
    parser = argparse.ArgumentParser(prog="opencode-pool", description="OpenCode 号池中转")
    parser.add_argument("--config", default=os.environ.get("OPENCODE_POOL_CONFIG", DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True)

    def add_config_argument(sp):
        sp.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_init = sub.add_parser("init", help="生成默认配置")
    p_init.add_argument("--force", action="store_true")
    p_init.add_argument("--token", default="")
    p_init.set_defaults(func=cmd_init)
    add_config_argument(p_init)

    for name, func, help_text in (
        ("check", cmd_check, "校验配置"),
        ("serve", cmd_serve, "起服务"),
        ("status", cmd_status, "读正在跑的池子"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.set_defaults(func=func)
        add_config_argument(sp)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
