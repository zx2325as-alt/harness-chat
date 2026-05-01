#!/usr/bin/env bash
# Harness Chat 一键启动（Linux / macOS）
# 用法: chmod +x start_all.sh && ./start_all.sh
# 对应 Windows 的 start_all.bat：后台启动后端 Uvicorn + 前端 Vue CLI，日志写入 logs/

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-6008}"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

BACKEND_LOG="$LOG_DIR/backend.log"
FRONTEND_LOG="$LOG_DIR/frontend.log"
BACKEND_PID_FILE="$LOG_DIR/backend.pid"
FRONTEND_PID_FILE="$LOG_DIR/frontend.pid"

banner() {
  echo "==================================================="
  echo "         Harness Chat Full-Stack Starter"
  echo "==================================================="
  echo ""
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "错误: 未找到命令「$1」，请先安装或加入 PATH。" >&2
    exit 1
  }
}

stop_if_pid_file() {
  local f="$1"
  local name="$2"
  if [[ -f "$f" ]]; then
    local pid
    pid="$(cat "$f" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "已在运行 ${name} (PID ${pid})，跳过重复启动。"
      return 0
    fi
  fi
  return 1
}

banner

need_cmd npm

if stop_if_pid_file "$BACKEND_PID_FILE" "后端"; then
  :
else
  echo "正在启动后端 (Uvicorn，端口 ${BACKEND_PORT})..."
  cd "$ROOT/backend"
  if [[ -x "$ROOT/backend/.venv/bin/uvicorn" ]]; then
    nohup "$ROOT/backend/.venv/bin/uvicorn" app:app --reload --port "$BACKEND_PORT" >>"$BACKEND_LOG" 2>&1 &
  elif command -v uvicorn >/dev/null 2>&1; then
    nohup uvicorn app:app --reload --port "$BACKEND_PORT" >>"$BACKEND_LOG" 2>&1 &
  else
    need_cmd python3
    nohup python3 -m uvicorn app:app --reload --port "$BACKEND_PORT" >>"$BACKEND_LOG" 2>&1 &
  fi
  echo $! >"$BACKEND_PID_FILE"
  cd "$ROOT"
fi

if stop_if_pid_file "$FRONTEND_PID_FILE" "前端"; then
  :
else
  echo "正在启动前端 (Vue CLI serve，端口 ${FRONTEND_PORT})..."
  cd "$ROOT/frontend"
  # 与 frontend/vue.config.js 默认一致；若需改端口: FRONTEND_PORT=3000 ./start_all.sh
  nohup npm run serve -- --port "$FRONTEND_PORT" >>"$FRONTEND_LOG" 2>&1 &
  echo $! >"$FRONTEND_PID_FILE"
  cd "$ROOT"
fi

echo ""
echo "服务已在后台启动（日志见 logs/）。"
echo ""
echo "  Backend API:  http://127.0.0.1:${BACKEND_PORT}"
echo "  Frontend UI: http://127.0.0.1:${FRONTEND_PORT} （首次编译可能需几秒）"
echo ""
echo "  查看日志: tail -f logs/backend.log    /    tail -f logs/frontend.log"
echo "  停止服务: ./stop_all.sh"
echo "==================================================="
