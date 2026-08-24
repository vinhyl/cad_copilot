#!/bin/bash
# CAD Copilot 后端服务 —— agent 托管的会话级生命周期控制（跨平台：macOS / Linux / Windows-GitBash）
# 用法（在工具 Bash 中调用）:
#   bash cad_service_ctl.sh status   # 检查服务是否在线
#   bash cad_service_ctl.sh start    # 若不在线上则拉起（需配合 run_in_background=true）
#   bash cad_service_ctl.sh open     # 在默认浏览器打开看图页面
#   bash cad_service_ctl.sh stop     # 主动关闭（释放 8764）
#
# 设计:服务不需要永久常驻,只在用户与 agent 交互一份图纸期间保持在线;
# 长时间不用被回收即可。start 必须在 run_in_background 任务中执行,
# 这样该后台任务即 python 进程本身,随会话存活、会话回收时自然退场。
#
# 跨平台 & 免硬编码:仓库根目录由本脚本自身位置自动推导(不再写死任何绝对路径),
# 因此同一份脚本可在任意设备 / 任意路径下直接使用;venv 解释器、打开浏览器命令、
# 停止命令都会按 OS 自动选择。

# ---- 仓库根目录:基于脚本所在目录自动推导(任意设备通用) ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$SCRIPT_DIR"

TOKEN="cad-local-dev-2026"
PORT=8764
URL="http://127.0.0.1:${PORT}/app/drawing.html?token=${TOKEN}"

# ---- 按 OS 选择 venv 解释器与"打开浏览器"命令 ----
OS="$(uname -s 2>/dev/null || echo unknown)"
case "$OS" in
  MINGW*|MSYS*|CYGWIN*|Windows_NT)
    PY="$REPO/venv/Scripts/python.exe"
    open_browser() { powershell -NoProfile -Command "Start-Process '$1'" >/dev/null 2>&1; }
    ;;
  *)
    PY="$REPO/venv/bin/python"
    open_browser() { open "$1" 2>/dev/null; }
    ;;
esac

up() { curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer ${TOKEN}" "http://127.0.0.1:${PORT}/api/config" 2>/dev/null; }

stop_server() {
  case "$OS" in
    MINGW*|MSYS*|CYGWIN*|Windows_NT)
      PID="$(netstat -ano 2>/dev/null | awk -v p=":$PORT" '$1=="TCP" && $2 ~ (p "$") && $4=="LISTENING" {print $5; exit}')"
      if [ -n "$PID" ]; then
        taskkill.exe /PID "$PID" /F >/dev/null 2>&1 && echo "已停止 ($PID)" || echo "停止失败,请手动结束 PID $PID"
      else
        echo "没有运行中的服务"
      fi
      ;;
    *)
      PID="$(lsof -ti :${PORT} 2>/dev/null)"
      if [ -n "$PID" ]; then kill $PID 2>/dev/null && echo "已停止 ($PID)"; else echo "没有运行中的服务"; fi
      ;;
  esac
}

case "${1:-status}" in
  status)
    code=$(up)
    if [ "$code" = "200" ]; then echo "UP   (HTTP 200)"; else echo "DOWN (HTTP ${code:-none})"; fi
    ;;
  start)
    code=$(up)
    if [ "$code" = "200" ]; then echo "already UP"; exit 0; fi
    echo "starting cad_service on :${PORT} …"
    cd "$REPO" || exit 1
    export CAD_SERVICE_TOKEN="$TOKEN"
    # exec:让本后台任务即 python 进程,随会话存活
    exec "$PY" cad_service.py
    ;;
  open)
    code=$(up)
    if [ "$code" != "200" ]; then echo "服务未启动(先运行 start)。链接: $URL"; fi
    open_browser "$URL"
    echo "已在浏览器打开: $URL"
    ;;
  stop)
    stop_server
    ;;
  *)
    echo "用法: $0 {status|start|open|stop}"; exit 1 ;;
esac
