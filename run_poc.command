#!/bin/bash
# 双击运行：在 Mac 上搭建并验证 POC，完整输出同时写入 logs/ 下的日志文件。
cd "$(dirname "$0")"
mkdir -p logs
LOG="logs/setup-$(date +%Y%m%d-%H%M%S).log"
echo "日志：$PWD/$LOG"
scripts/setup_mac.sh "$@" 2>&1 | tee "$LOG"
echo
echo "运行结束，可以关闭这个窗口。"
read -r -n 1 -s -p "按任意键退出…"
