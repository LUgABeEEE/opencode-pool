#!/usr/bin/env bash
# 重启后自检：中转活着 / 插件路由注册 / dsh-web 在线
set -u
HOME_DIR="${OPENCODE_POOL_HOME:-$HOME/.dsh/opencode-pool}"

echo "== 中转 =="
( cd "$(dirname "$0")/.." && python3 -m opencode_pool --config "$HOME_DIR/pool.config.json" status ) 2>&1 | head -8
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8790/healthz)
echo "relay healthz = $code"

echo
echo "== dsh-web =="
pid=$(systemctl show -p MainPID --value dsh-web 2>/dev/null)
echo "MainPID = ${pid:-读不到}"
echo "HTTP     = $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3080/)（401 = 鉴权正常）"

echo
echo "== 插件 =="
if command -v journalctl >/dev/null 2>&1; then
  hits=$(journalctl -u dsh-web --since '-10 min' --no-pager 2>/dev/null | grep -c "opencode-pool")
  echo "近 10 分钟日志里 opencode-pool 相关行：$hits"
  journalctl -u dsh-web --since '-10 min' --no-pager 2>/dev/null | grep "opencode-pool" | tail -5
fi
if [ -f /data/agent/logs/dsh-web-restart.log ]; then
  echo "重启日志尾部："; tail -6 /data/agent/logs/dsh-web-restart.log
fi

echo
echo "下一步：浏览器刷新 http://127.0.0.1:3080 ，侧边栏应出现「OpenCode 号池」"
