#!/bin/bash
# 一键启动 OKX 价格行为信号仪表盘
# 用法: ./start_dashboard.sh    （前台运行，Ctrl+C 停止）
#       ./start_dashboard.sh -d （后台运行，日志写入 /tmp/dash.log，可用 ./stop_dashboard.sh 停止）
# 说明: 凭证从项目 .env 自动加载，无需手动 export
set -e
cd "$(dirname "$0")"

DEBUG_FLAG=""
if [ "$1" = "-d" ]; then
  DEBUG_FLAG="-d"
fi

if [ "$DEBUG_FLAG" = "-d" ]; then
  echo "后台启动中... 日志: /tmp/dash.log"
  DASH_DEBUG=0 nohup ./.venv/bin/python -m web_dashboard.app > /tmp/dash.log 2>&1 &
  echo "已后台启动 (PID $!)，访问 http://127.0.0.1:8000"
  echo "停止: ./stop_dashboard.sh"
else
  echo "前台启动中... 访问 http://127.0.0.1:8000，Ctrl+C 停止"
  DASH_DEBUG=0 exec ./.venv/bin/python -m web_dashboard.app
fi
