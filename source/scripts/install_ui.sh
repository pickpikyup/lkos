#!/bin/bash
# 启动前检查（LKOS.app 每次启动时调用，很快）：
#   1. 本程序以“可编辑”方式装在 .venv 里，并指向 source/（目录调整后自动重新登记，不联网）
#   2. 窗口界面组件 pywebview（只装一次，需要联网；PyPI 慢时可设 PIP_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple）
cd "$(dirname "$0")/../.."   # 项目根目录
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
PY=.venv/bin/python
pip_install() {
  if command -v uv >/dev/null 2>&1; then uv pip install --python "$PY" "$@"; else "$PY" -m pip install "$@"; fi
}
if ! "$PY" -c "import lkos, sys; sys.exit(0 if '/source/lkos/' in lkos.__file__ else 1)" 2>/dev/null; then
  echo "重新登记程序位置（source/）…"
  pip_install --no-deps -e source || exit 1
fi
if ! "$PY" -c "import webview" 2>/dev/null; then
  echo "首次启动：安装窗口界面组件 pywebview…"
  INDEX=()
  [ -n "$PIP_MIRROR" ] && INDEX=(--index-url "$PIP_MIRROR")
  pip_install "${INDEX[@]}" "pywebview>=5" || exit 1
fi
"$PY" -c "import lkos, webview" 2>/dev/null
