#!/usr/bin/env bash
# 可复现的测试稳定性证据（t10）：单次快照不算数。
#   1) 全量 discover × N
#   2) ReadinessTest 单跑 × N（历史抖动点）
#   3) 每个测试文件单独跑
# 用法：./scripts/verify.sh [N]   # 默认 N=20
set -uo pipefail
cd "$(dirname "$0")/.."

ROUNDS="${1:-20}"
fail=0

section() { printf '\n== %s ==\n' "$1"; }

section "连续 ${ROUNDS} 次全量 discover"
for i in $(seq 1 "$ROUNDS"); do
  out=$(python3 -m unittest discover -s tests 2>&1 | tail -1)
  echo "  #$i $out"
  echo "$out" | grep -q '^OK' || fail=1
done

section "ReadinessTest 单跑 ${ROUNDS} 次"
for i in $(seq 1 "$ROUNDS"); do
  out=$(python3 -m unittest tests.test_opencode_pool.ReadinessTest 2>&1 | tail -1)
  echo "  #$i $out"
  echo "$out" | grep -q '^OK' || fail=1
done

section "每个测试文件单独跑"
for f in tests/test_*.py; do
  module=$(printf '%s' "${f%.py}" | tr '/' '.')
  out=$(python3 -m unittest "$module" 2>&1 | tail -1)
  echo "  $(basename "$f"): $out"
  echo "$out" | grep -q '^OK' || fail=1
done

printf '\n'
if [ "$fail" -eq 0 ]; then
  echo "RESULT: STABLE"
else
  echo "RESULT: FLAKY"
fi
exit "$fail"
