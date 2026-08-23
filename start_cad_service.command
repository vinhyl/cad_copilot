#!/bin/bash
# 双击本文件即可在终端中运行，由 launchd 常驻托管 CAD Copilot 后端服务。
# 托管后：崩溃自动复活、登录自动启动，不再因会话结束而关闭。
PLIST=~/Library/LaunchAgents/com.cadcopilot.service.plist

# 若已加载先卸载（忽略错误），保证幂等
launchctl unload "$PLIST" 2>/dev/null

# 释放 8764 端口：停掉可能存在的旧手动实例（工具后台起的临时进程等）
PID=$(lsof -ti :8764 2>/dev/null)
if [ -n "$PID" ]; then
  echo "释放占用 8764 的残留进程 ($PID)…"
  kill $PID 2>/dev/null
  sleep 1
fi

# 由 launchd 常驻托管：崩溃自动复活、登录自动启动
launchctl load "$PLIST"

sleep 2

CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer cad-local-dev-2026" http://127.0.0.1:8764/api/config)
echo "服务状态: HTTP $CODE"
if [ "$CODE" = "200" ]; then
  echo "✅ 服务已常驻启动（launchd 托管，崩溃/重启后会自动恢复，不再自动关闭）"
  echo
  echo "打开看图页面："
  echo "  http://127.0.0.1:8764/app/drawing.html?token=cad-local-dev-2026"
else
  echo "⚠️ 启动异常，请查看日志："
  echo "  /tmp/cad_service.log"
fi
echo
echo "关闭此窗口即可（服务在后台继续运行，不受窗口关闭影响）。"
read -n 1 -s -r -p "按任意键退出…"
