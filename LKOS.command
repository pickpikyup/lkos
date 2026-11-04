#!/bin/bash
# 双击打开本地知识库窗口（终端会显示运行日志，出问题时方便排查）。
# 想在终端里用命令行问答：bin/lkos
cd "$(dirname "$0")"
scripts/install_ui.sh || { echo "安装窗口组件失败，请检查网络后重试（或设置 PIP_MIRROR）。"; read -r -n 1 -s -p "按任意键退出…"; exit 1; }
pgrep -xq Ollama || pgrep -xq ollama || { open -a Ollama 2>/dev/null; sleep 3; }
bin/lkos app
