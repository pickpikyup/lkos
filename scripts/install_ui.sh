#!/bin/bash
# 安装窗口界面组件 pywebview（只需一次）。PyPI 慢时可设 PIP_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if .venv/bin/python -c "import webview" 2>/dev/null; then
  exit 0
fi
echo "首次启动：安装窗口界面组件 pywebview…"
INDEX=()
[ -n "$PIP_MIRROR" ] && INDEX=(--index-url "$PIP_MIRROR")
if command -v uv >/dev/null 2>&1; then
  uv pip install --python .venv/bin/python "${INDEX[@]}" "pywebview>=5"
else
  .venv/bin/python -m pip install "${INDEX[@]}" "pywebview>=5"
fi
.venv/bin/python -c "import webview" 2>/dev/null
