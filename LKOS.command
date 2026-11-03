#!/bin/bash
# 双击打开本地知识库问答。直接输入问题回车；/help 查看命令；/quit 退出。
cd "$(dirname "$0")"
pgrep -xq Ollama || pgrep -xq ollama || { open -a Ollama 2>/dev/null; sleep 3; }
bin/lkos
