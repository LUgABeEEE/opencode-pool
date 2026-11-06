#!/usr/bin/env bash
# 启停 opencode-pool 中转（无 systemd 时的托管方式）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="${OPENCODE_POOL_HOME:-$HOME/.dsh/opencode-pool}"
CFG="$HOME_DIR/pool.config.json"
PIDF="$HOME_DIR/relay.pid"
mkdir -p "$HOME_DIR"

status_line() {
  python3 -m opencode_pool --config "$CFG" status 2>/dev/null || echo "（读不到状态）"
}

case "${1:-start}" in
  start)
    # 以"端口真的在服务"为准，而不是 pid 文件 —— pid 文件会被插件拉起的实例覆盖，
    # 陈旧的 pid 会让这里以为没在跑，于是起第二个实例并撞 Address already in use。
    # 用"端口是否在监听"判存活，不要用 /healthz：
    # /healthz 是就绪语义（全池冷却/额度耗尽会 503），拿它判存活会把"起来了但没号"误判成"没起来"。
    if ss -ltn 2>/dev/null | grep -q ':8790 '; then
      echo "已在运行（端口 8790 在监听）"; exit 0
    fi
    ORPHAN=$(ss -ltnp 2>/dev/null | grep ':8790 ' | grep -o 'pid=[0-9]*' | cut -d= -f2 | head -1 || true)
    if [[ -n "${ORPHAN:-}" ]]; then
      echo "发现占用 8790 但未健康的进程 $ORPHAN，先收掉"
      kill "$ORPHAN" 2>/dev/null || true
      sleep 1
    fi
    [[ -f $CFG ]] || python3 -m opencode_pool --config "$CFG" init
    cd "$ROOT"
    setsid nohup python3 -m opencode_pool serve --config "$CFG" >> "$HOME_DIR/relay.log" 2>&1 &
    for _ in $(seq 1 40); do
      sleep 0.25
      if ss -ltn 2>/dev/null | grep -q ':8790 '; then
        echo "已启动（端口已监听）"
        # 区分三种情况：健康 / 首轮采样未落定（等几秒就 200）/ 真的没有可调度账号
        reason=""
        for _ in $(seq 1 16); do
          body=$(curl -s --max-time 2 http://127.0.0.1:8790/healthz 2>/dev/null)
          if [ -n "$body" ]; then
            code=$(printf '%s' "$body" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("ready"),d.get("reason"))' 2>/dev/null)
            case "$code" in
              "True ready") echo "健康检查通过"; break ;;
              *usage-not-sampled*) reason="首轮额度采样还没落定（正常，几秒后会转 ready）" ;;
              *) reason="没有可调度账号（reason=$(printf '%s' "$code" | awk '{print $2}')，看 ./scripts/start.sh status）" ;;
            esac
          fi
          sleep 0.5
        done
        [ -n "$reason" ] && echo "注意：/healthz 未 ready —— $reason"
        exit 0
      fi
    done
    echo "启动失败，日志："; tail -20 "$HOME_DIR/relay.log"; exit 1
    ;;
  stop)
    if [[ -f $PIDF ]] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
      kill "$(cat "$PIDF")"; sleep 1; rm -f "$PIDF"; echo "已停止"
    else echo "没在跑"; fi
    ;;
  status) status_line ;;
  *) echo "用法: $0 {start|stop|status}"; exit 2 ;;
esac
