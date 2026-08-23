#!/bin/bash
# CAD Copilot 后端服务 —— agent 托管的会话级生命周期控制
# 用法（在工具 Bash 中调用）:
#   bash cad_service_ctl.sh status   # 检查服务是否在线
#   bash cad_service_ctl.sh start    # 若不在线上则拉起（需配合 run_in_background=true）
#   bash cad_service_ctl.sh open     # 在默认浏览器打开看图页面
#   bash cad_service_ctl.sh stop     # 主动关闭（释放 8764）
#
# 设计:服务不需要永久常驻,只在用户与 agent 交互一份图纸期间保持在线;
# 长时间不用被回收即可。start 必须在 run_in_background 任务中执行,
# 这样该后台任务即 python 进程本身,随会话存活、会话回收时自然退场。

REPO="/Users/vvvvvv/Documents/ai projects/cad_copilot"
TOKEN="cad-local-dev-2026"
PORT=8764
URL="http://127.0.0.1:${PORT}/app/drawing.html?token=${TOKEN}"

up() { curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer ${TOKEN}" "http://127.0.0.1:${PORT}/api/config" 2>/dev/null; }

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
    exec "$REPO/venv/bin/python" cad_service.py
    ;;
  open)
    code=$(up)
    if [ "$code" != "200" ]; then echo "服务未启动(先运行 start)。链接: $URL"; fi
    open "$URL" 2>/dev/null && echo "已在浏览器打开: $URL" || echo "无法自动打开,请手动访问: $URL"
    ;;
  stop)
    PID=$(lsof -ti :${PORT} 2>/dev/null)
    if [ -n "$PID" ]; then kill $PID 2>/dev/null && echo "已停止 ($PID)"; else echo "没有运行中的服务"; fi
    ;;
  *)
    echo "用法: $0 {status|start|open|stop}"; exit 1 ;;
esac
