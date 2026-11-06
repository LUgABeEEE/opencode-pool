"""无界面的端到端自检：把"能不能真的跑起来"证明一遍。

不碰用户真实配置：全程在临时目录里造一份 pool.config.json，用随机端口起
一次真中转，验证 加号 → 起服务 → 拉速度 → 读日志 → 停止 这条链。
GUI 部分只验证 tkinter 能被创建，不弹窗口。

    python selftest.py            # 全跑
    python selftest.py --keep     # 跑完保留临时目录（排障用）
"""
from __future__ import annotations

import json
import os
import random
import shutil
import socket
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 只硬导入"第一层就存在"的模块（外壳基础设施 + 内核托管）。
# 页面 / clientcfg 这些后加的东西在各测试函数里惰性导入，本层还没有就记为 SKIP ——
# 这样同一份自检能在每个分层 PR 上都跑绿。
from opencodepool_client import (icon, localcfg, logtail, paths,  # noqa: E402
                                 relay, theme, util)

PROJECT = os.path.dirname(HERE)
RESULTS: list[tuple[str, str, str]] = []      # (name, ok|fail|skip, detail)


def check(name: str, ok: bool, detail: str = "") -> bool:
    status = "ok" if ok else "fail"
    RESULTS.append((name, status, detail))
    mark = "OK  " if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f"  —  {detail}"
    print(line)
    return ok


def skip(name: str, why: str) -> None:
    RESULTS.append((name, "skip", why))
    print(f"[SKIP] {name}  —  {why}")


def have(module: str):
    """惰性导入一个后加模块：不在本层就返回 None。"""
    import importlib

    try:
        return importlib.import_module(f"opencodepool_client.{module}")
    except Exception:  # noqa: BLE001
        return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeSettings:
    """够 RelayProcess 用的最小 settings（避免写真实 client.json）。"""

    def __init__(self, project: str, config_path: str, python: str):
        self._project = project
        self._config = config_path
        self._python = python
        self._log = os.path.join(os.path.dirname(config_path), "relay.log")

    def project_dir(self) -> str:
        return self._project

    def config_path(self) -> str:
        return self._config

    def python(self) -> str:
        return self._python

    def relay_log(self) -> str:
        return self._log

    def refresh_seconds(self) -> int:
        return 2

    def request_rows(self) -> int:
        return 200

    def __getitem__(self, key):
        return {"start_service_on_launch": False, "stop_service_on_exit": True}.get(key)


def test_imports() -> None:
    base = [m for m in (icon, localcfg, logtail, paths, relay, theme, util) if m is not None]
    check("基础模块导入", len(base) == 7, f"{len(base)}/7")
    gui = {name: have(name) for name in ("tray", "widgets", "settings")}
    check("外壳基础设施导入", all(m is not None for m in gui.values()),
          ", ".join(f"{k}={'ok' if v else 'miss'}" for k, v in gui.items()))

    if have("app") is None:
        skip("主窗口导入", "本层还没有 app.py")
    else:
        check("主窗口导入", True, "app")

    pages = [name for name in ("dashboard", "accounts", "connect", "requests", "settings_page")
             if have(f"pages.{name}") is not None]
    if pages:
        check("页面模块导入", True, ", ".join(pages))
    else:
        skip("页面模块导入", "本层还没有 pages/")


def test_icon(tmp: str) -> None:
    try:
        path = icon.build_ico(os.path.join(tmp, "app.ico"))
        size = os.path.getsize(path) if path else 0
        check("生成 .ico", bool(path) and size > 2048, f"{size} 字节")
    except Exception as exc:  # noqa: BLE001
        check("生成 .ico", False, f"{type(exc).__name__}: {exc}")


def test_localcfg(tmp: str, port: int) -> str:
    config = os.path.join(tmp, "pool.config.json")
    try:
        raw, created = localcfg.ensure_config(config)
        check("生成默认配置", created and raw.get("relay_token", "").startswith("pool-"),
              f"token {len(raw.get('relay_token',''))} 字符")
        raw["listen"] = f"127.0.0.1:{port}"
        raw["usage_refresh_seconds"] = 30
        localcfg.save_config(config, raw)

        info = localcfg.add_account(config, "selftest-01", "opencode-go", "sk-selftest-fake-key")
        check("离线加号", bool(info.get("slot")), info.get("slot", ""))

        secrets_file = localcfg.secrets_path(config)
        described = localcfg.secret_describe(secrets_file, info["slot"])
        check("密钥落盘且能算指纹", described["configured"] and len(described["fingerprint"]) == 8,
              described["fingerprint"])

        reloaded = localcfg.load_config(config)
        names = [a.get("name") for a in localcfg.accounts(reloaded)]
        check("账号写进配置", names == ["selftest-01"], str(names))
        check("会话前缀自动生成",
              reloaded["accounts"][0].get("session_prefix") == "dsh-pool-selftest-01",
              str(reloaded["accounts"][0].get("session_prefix")))

        backup = localcfg.rotate_key(config, "selftest-01", "sk-selftest-fake-key-2")
        check("换 key", bool(backup.get("slot")),
              localcfg.secret_describe(secrets_file, backup["slot"])["fingerprint"])

        localcfg.set_enabled(config, "selftest-01", False)
        check("暂停账号", localcfg.find_account(localcfg.load_config(config), "selftest-01").get("enabled") is False)
        localcfg.set_enabled(config, "selftest-01", True)

        options = localcfg.options_snapshot(config)
        check("参数快照", "min_headroom" in options and "sticky_ttl_seconds" in options,
              f"min_headroom={options.get('min_headroom')}")

        localcfg.update_options(config, {"min_headroom": 5.0})
        check("改参数", localcfg.options_snapshot(config)["min_headroom"] == 5.0)
        localcfg.update_options(config, {"min_headroom": 2.0})

        try:
            localcfg.add_account(config, "bad name!", "opencode-go", "x")
            check("非法账号名被拦", False)
        except localcfg.LocalConfigError:
            check("非法账号名被拦", True)
        return config
    except Exception:  # noqa: BLE001
        check("localcfg 流程", False, traceback.format_exc(limit=2))
        return config


def test_service(tmp: str, config: str, port: int) -> None:
    settings = FakeSettings(PROJECT, config, sys.executable)
    proc = relay.RelayProcess(settings)
    client = relay.RelayClient(config)
    check("base_url 与配置一致", client.base_url() == f"http://127.0.0.1:{port}", client.base_url())

    ok, message = proc.start(wait_seconds=25)
    check("起中转（真子进程）", ok, message.replace("\n", " ")[:150])
    if not ok:
        proc.stop()
        return
    try:
        check("healthz 就绪", client.healthy())

        ok_ready, body = client.readiness()
        check("readiness 结构", isinstance(body, dict) and "accounts" in body and "reason" in body,
              f"reason={body.get('reason')} ready={body.get('ready')}")

        snap = client.snapshot()
        required = {"accounts", "healthy_accounts", "total_accounts", "pool_worst",
                    "upstreams", "requests_last_5m"}
        check("snapshot 字段齐全", required.issubset(set(snap)),
              f"accounts={len(snap.get('accounts') or [])}")
        account = (snap.get("accounts") or [{}])[0]
        check("账号视图带三档窗口与状态",
              "windows" in account and "percent" in account and "key" in account,
              f"windows={list((account.get('windows') or {}).keys())}")
        check("假 key 被上游拒绝但不影响进程",
              bool(account.get("refresh_error")) or account.get("percent"),
              str(account.get("refresh_error"))[:80])

        schema = client.schema()
        windows = []
        for info in (schema.get("providers") or {}).values():
            windows = [w.get("key") for w in (info.get("windows") or [])]
            break
        check("schema 声明三档窗口", windows == ["rolling", "weekly", "monthly"], str(windows))

        blocked = client.clear_cooldowns()
        check("清冷却接口", "cleared" in blocked, str(blocked))

        reload_result = client.reload()
        check("热重载接口", reload_result.get("ok") is True, str(reload_result.get("accounts"))[:80])

        added = client.add_account("selftest-02", "opencode-go", "sk-selftest-second")
        check("在线加号（走 HTTP）", added.get("ok") is True, str(added.get("slot")))
        removed = client.remove_account("selftest-02")
        check("在线删号", removed.get("ok") is True)

        tail = proc.tail(20)
        check("日志管道有输出", bool(tail), f"{len(tail)} 行，最后一行：{tail[-1][:80] if tail else ''}")
        check("relay.log 落盘", os.path.isfile(settings.relay_log())
              and os.path.getsize(settings.relay_log()) > 0,
              f"{os.path.getsize(settings.relay_log())} 字节")
    finally:
        ok, message = proc.stop()
        check("停止中转", ok and not client.healthy(), message)
        check("停止后端口释放", not client.healthy())


def test_logtail(tmp: str) -> None:
    path = os.path.join(tmp, "requests.jsonl")
    now = time.time()
    rows = []
    for index in range(12):
        rows.append({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - index * 60)),
            "req_id": f"{index:08x}",
            "model": "deepseek-v4.1-flash",
            "account": "selftest-01" if index % 2 else "selftest-02",
            "path": "/v1/chat/completions",
            "status": 200 if index % 4 else 429,
            "outcome": "ok" if index % 4 else "throttled",
            "total_ms": 800 + index * 50,
            "ttfb_ms": 300 + index * 20,
            "failovers": 1 if index % 4 == 0 else 0,
            "tokens": {"input": 120, "cache_read": 60 if index % 2 else 0, "output": 30},
        })
    with open(path, "w", encoding="utf-8") as f:
        for row in reversed(rows):          # 真实日志是 append：越往后越新
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    records = logtail.read_records(path, limit=10)
    check("读请求日志尾部", len(records) == 10, f"{len(records)} 条（写 12 条，取 10）")
    check("最新一条在最前", records[0].get("req_id") == "00000000", records[0].get("req_id"))
    summary = logtail.summarize(records)
    check("统计聚合", summary.get("total") == 10 and 0 < summary.get("ok_ratio", 0) <= 1,
          f"total={summary.get('total')} ok={summary.get('ok_ratio'):.2f} "
          f"p50={summary.get('p50_ms')} cache={summary.get('cache_ratio')}")
    row = logtail.record_row(records[0])
    check("表格行 8 列", len(row) == 8, str(row))
    check("tail_text 读散文日志", logtail.tail_text(os.path.join(tmp, "nope.log")) == [])


def test_clientcfg() -> None:
    clientcfg = have("clientcfg")
    if clientcfg is None:
        skip("接驳写入（Codex 片段）", "本层还没有 clientcfg.py")
        return
    content, created = clientcfg.preview_codex("http://127.0.0.1:8790", "deepseek-v4.1-flash")
    has_block = "[model_providers.opencode-pool]" in content
    check("Codex 片段生成", has_block and "127.0.0.1:8790/v1" in content,
          f"created={created} lines={len(content.splitlines())}")

    stripped = clientcfg._strip_our_block(content)
    check("Codex 片段可摘除（还原用）",
          "[model_providers.opencode-pool]" not in stripped
          and 'model_provider = "opencode-pool"' not in stripped)

    snippets = clientcfg.snippets("http://127.0.0.1:8790", "pool-token", "m")
    check("五种接驳片段", len(snippets) == 5 and all("8790" in v for v in snippets.values()),
          ", ".join(snippets))
    values = clientcfg.env_preview("http://127.0.0.1:8790", "tok", ("OPENAI_BASE_URL",))
    check("环境变量预览", values.get("OPENAI_BASE_URL") == "http://127.0.0.1:8790/v1", str(values))


def test_util() -> None:
    check("格式化工具",
          util.fmt_seconds(120) == "2 分" and util.fmt_countdown(time.time() + 7300).endswith("m"),
          f"{util.fmt_seconds(120)} / {util.fmt_countdown(time.time() + 7300)}")
    status = util.account_status({"enabled": True, "key": {"configured": True},
                                  "headroom": 40, "percent": {"rolling": 60}}, True)
    check("状态推导", status[0] == "ready", str(status[:2]))
    cool = util.account_status({"enabled": True, "key": {"configured": True},
                                "cooldown_until": time.time() + 60,
                                "cooldown_reason": "429 weekly"}, True)
    check("冷却状态优先", cool[0] == "cooling", str(cool[:2]))
    offline = util.account_status({}, False)
    check("离线状态", offline[0] == "offline", str(offline[:2]))


def test_single_instance() -> None:
    from opencodepool_client import single_instance

    first = single_instance.SingleInstance("Local\\OpenCodePoolClient.SelfTest")
    second = single_instance.SingleInstance("Local\\OpenCodePoolClient.SelfTest")
    check("单实例互斥", first.available and first.already_running is False
          and second.already_running is True,
          f"available={first.available} second_running={second.already_running}")
    first.release()
    second.release()


class FakeApp:
    """页面只用 app 的几个字段，造一个假的比起真内核快 100 倍。"""

    def __init__(self, settings, snap: dict, healthy: bool = True):
        self.settings = settings
        self.snap = snap
        self.schema: dict = {}
        self.healthy = healthy
        self.svc_text = "已就绪（测试）"
        self.svc_state = "running"
        self.current_page = "dashboard"
        self.client = None
        self.toasts: list[tuple[str, str]] = []

    def toast(self, message: str, kind: str = "info") -> None:
        self.toasts.append((kind, message))

    def _page_subtitle(self, _text: str) -> None:
        pass

    def confirm(self, *_a, **_k) -> bool:
        return False

    def refresh_now(self) -> None:
        pass

    def refresh_quota(self) -> None:
        pass

    def _broadcast(self) -> None:
        pass

    def run_async(self, *_a, **_k) -> None:   # 渲染测试不该真的起线程
        pass

    def open_panel(self) -> None:
        pass


def test_ui_render(tmp: str, config: str) -> None:
    """用构造出来的 snapshot 渲染页面，抓"数据形状一变就崩"的回归。"""
    dashboard = have("pages.dashboard")
    if dashboard is None:
        skip("仪表盘渲染", "本层还没有 pages/dashboard.py")
        return
    try:
        import tkinter as tk
    except Exception as exc:  # noqa: BLE001
        skip("仪表盘渲染", f"没有可用的 tkinter：{type(exc).__name__}: {exc}")
        return

    settings = FakeSettings(PROJECT, config, sys.executable)
    now = time.time()
    snap = {
        "upstreams": {"opencode-go": {"windows": [
            {"key": "rolling", "label": "5 小时"},
            {"key": "weekly", "label": "周"},
            {"key": "monthly", "label": "月"},
        ]}},
        "healthy_accounts": 1,
        "total_accounts": 2,
        "requests_last_5m": 7,
        "pool_worst": {"opencode-go": {
            "rolling": {"percent": 88.0, "account": "a2"},
            "weekly": {"percent": 60.0, "account": "a2"},
        }},
        "accounts": [
            {"name": "a1", "provider": "opencode-go", "enabled": True, "headroom": 55.0,
             "percent": {"rolling": 45.0, "weekly": 20.0, "monthly": 10.0},
             "binding_window": "rolling", "windows": {},
             "key": {"configured": True, "fingerprint": "abcd1234"},
             "requests": 12, "errors": 0, "cache_hit_ratio": 0.42,
             "last_refresh": now - 30},
            {"name": "a2", "provider": "opencode-go", "enabled": True, "headroom": 12.0,
             "percent": {"rolling": 88.0, "weekly": 60.0, "monthly": 40.0},
             "binding_window": "rolling", "windows": {},
             "key": {"configured": True, "fingerprint": "ffff0000"},
             "requests": 3, "errors": 1, "cache_hit_ratio": 0.1,
             "cooldown_until": 0, "last_refresh": now - 5},
        ],
    }

    root = tk.Tk()
    root.withdraw()
    keeper = theme.Theme(root)
    keeper.apply(root)
    try:
        app = FakeApp(settings, snap)
        page = dashboard.DashboardPage(root, app)
        page.pack(fill="both", expand=True)
        page.on_show()
        root.update_idletasks()
        check("仪表盘渲染两张账号卡", len(page._cards) == 2, str(list(page._cards)))
        card = page._cards.get("a2")
        check("额度环取到真实百分比",
              card is not None and card.rings["rolling"].value == 88.0,
              f"rolling={getattr(card.rings['rolling'], 'value', None) if card else None}")
        check("统计卡显示 1/2", page.stat_labels["ready"].cget("text") == "1 / 2",
              page.stat_labels["ready"].cget("text"))
        check("池内最坏点进窗口环",
              page._window_rings["rolling"].value == 88.0 and
              "a2" in page._window_caption["rolling"].cget("text"),
              page._window_caption["rolling"].cget("text"))
        check("余量条按 headroom 上色",
              card.meter.value == 12.0, str(card.meter.value))

    except Exception:  # noqa: BLE001
        check("UI 渲染", False, traceback.format_exc(limit=2))
    finally:
        root.destroy()



def test_requests_render(tmp: str, config: str) -> None:
    """请求页：拿一份真 jsonl 渲染，验证行数 / 成功率 / 详情抽屉。"""
    requests_page = have("pages.requests")
    if requests_page is None:
        skip("请求页渲染", "本层还没有 pages/requests.py")
        return
    try:
        import tkinter as tk
    except Exception as exc:  # noqa: BLE001
        skip("请求页渲染", f"没有可用的 tkinter：{type(exc).__name__}: {exc}")
        return
    now = time.time()
    root = tk.Tk()
    root.withdraw()
    keeper = theme.Theme(root)
    keeper.apply(root)
    app = FakeApp(FakeSettings(PROJECT, config, sys.executable), {})
    try:
        # 请求页：用一份真 jsonl
        log_path = os.path.join(tmp, "ui-requests.jsonl")
        with open(log_path, "w", encoding="utf-8") as f:
            for index in range(5):
                f.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - index * 10)),
                    "req_id": f"{index:08x}", "model": "deepseek-v4.1-flash",
                    "account": "a1" if index % 2 else "a2",
                    "path": "/v1/chat/completions",
                    "status": 200 if index else 429,
                    "outcome": "ok" if index else "throttled",
                    "total_ms": 900, "ttfb_ms": 300, "failovers": 0,
                    "tokens": {"input": 100, "cache_read": 40},
                }, ensure_ascii=False) + "\n")
        raw = localcfg.load_config(config)
        raw["request_log_path"] = log_path
        localcfg.save_config(config, raw)

        req_page = requests_page.RequestsPage(root, app)
        req_page.pack(fill="both", expand=True)
        req_page.on_show()
        root.update_idletasks()
        rows = req_page.tree.get_children()
        check("请求页读到 5 行", len(rows) == 5, f"{len(rows)} 行")
        check("统计条算出成功率", req_page.stat_labels["ok"].cget("text") == "80%",
              req_page.stat_labels["ok"].cget("text"))
        req_page.tree.selection_set(rows[-1])
        root.update()          # <<TreeviewSelect>> 要走一遍事件循环才会到
        root.update()
        check("详情面板展开字段", "req_id" in req_page.detail.cget("text"),
              req_page.detail.cget("text")[:60])
    except Exception:  # noqa: BLE001
        check("请求页渲染", False, traceback.format_exc(limit=2))
    finally:
        root.destroy()


def test_tk() -> None:
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        # Theme 必须持住：它建的 tkfont.Font 一旦被 GC，Tcl 字体命令就没了，
        # 之后任何用这套 style 的控件都会报 invalid command name。
        theme_keeper = theme.Theme(root)
        theme_keeper.apply(root)
        from opencodepool_client import widgets

        ring = widgets.Ring(root, size=60)
        ring.set(42.0)
        meter = widgets.Meter(root, width=100)
        meter.set(58.0)
        root.update_idletasks()
        value = ring.value
        root.destroy()
        check("tkinter 可用 + 控件绘制", value == 42.0, "Ring/Meter 正常")
    except Exception as exc:  # noqa: BLE001
        if "display" in str(exc).lower() or "Tcl" in type(exc).__name__:
            skip("tkinter 可用 + 控件绘制", f"没有可用的图形会话：{exc}")
        else:
            check("tkinter 可用 + 控件绘制", False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    random.seed()
    port = free_port()
    keep = "--keep" in sys.argv
    tmp = tempfile.mkdtemp(prefix="opencode-pool-selftest-")
    print(f"临时目录：{tmp}\n项目目录：{PROJECT}\n端口：{port}\n" + "-" * 70)
    try:
        test_imports()
        test_icon(tmp)
        config = test_localcfg(tmp, port)
        test_service(tmp, config, port)
        test_logtail(tmp)
        test_clientcfg()
        test_util()
        test_single_instance()
        test_ui_render(tmp, config)
        test_requests_render(tmp, config)
        test_tk()
    except Exception:  # noqa: BLE001
        check("自检崩溃", False, traceback.format_exc(limit=3))
    finally:
        failed = [r for r in RESULTS if r[1] == "fail"]
        skipped = [r for r in RESULTS if r[1] == "skip"]
        print("-" * 70)
        print(f"共 {len(RESULTS)} 项：通过 {len(RESULTS) - len(failed) - len(skipped)}，"
              f"跳过 {len(skipped)}，失败 {len(failed)}")
        if keep:
            print(f"保留临时目录：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return 1 if [r for r in RESULTS if r[1] == "fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
