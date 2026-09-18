#!/bin/bash
# 停止后台运行的仪表盘服务
pkill -f "web_dashboard.app" 2>/dev/null
echo "已请求停止仪表盘服务"
