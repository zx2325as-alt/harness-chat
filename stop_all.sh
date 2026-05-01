#!/usr/bin/env bash
# 停止 start_all.sh 启动的后端 / 前端进程（按 logs/*.pid）

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT/logs"

kill_pid_file() {
  local f="$1"
  local name="$2"
  if [[ ! -f "$f" ]]; then
    echo "未找到 ${name} PID 文件: $f（可能未启动）"
    return 0
  fi
  local pid
  pid="$(cat "$f" 2>/dev/null || true)"
  if [[ -z "${pid:-}" ]]; then
    rm -f "$f"
    return 0
  fi
  if kill -0 "$pid" 2>/dev/null; then
    echo "停止 ${name} (PID ${pid})..."
    kill "$pid" 2>/dev/null || true
    sleep 0.5
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  else
    echo "${name} PID ${pid} 已不存在，清理 PID 文件。"
  fi
  rm -f "$f"
}

kill_pid_file "$LOG_DIR/frontend.pid" "前端"
kill_pid_file "$LOG_DIR/backend.pid" "后端"
echo "完成。"
