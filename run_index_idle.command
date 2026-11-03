#!/bin/bash
# 双击运行：闲时索引 data/test-docs（数据目录 data/realdocs-v2）。
# 你在用电脑、正在问答、或使用电池时自动暂停；条件满足后继续。窗口可以一直开着，Ctrl-C 随时停止，下次运行会续传。
cd "$(dirname "$0")"
mkdir -p logs
export LKOS_HOME="$PWD/data/realdocs-v2" PYTHONDONTWRITEBYTECODE=1 COLUMNS=160
FOLDER="${1:-data/test-docs}"
.venv/bin/lkos index "$FOLDER" --when-idle 2>&1 | tee "logs/index-idle-$(date +%Y%m%d-%H%M%S).log"
read -r -n 1 -s -p "按任意键退出…"
