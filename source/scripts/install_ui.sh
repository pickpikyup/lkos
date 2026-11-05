#!/bin/bash
# 启动前检查（LKOS.app 每次启动时调用，很快）：
#   1. 本程序以“可编辑”方式装在 .venv 里，并指向 source/（目录调整后自动重新登记，不联网）
#   2. 缺少的依赖（窗口界面 pywebview、PPTX 解析 python-pptx、文字识别 pyobjc Vision 等；只装一次，需要联网；
#      PyPI 慢时可设 PIP_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple）
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
# 新版本增加了依赖（如 PPTX 解析用的 python-pptx）或第一次启动：安装缺少的组件（需要联网，只装一次）
INDEX=()
[ -n "$PIP_MIRROR" ] && INDEX=(--index-url "$PIP_MIRROR")
if ! "$PY" -c "import webview, pptx" 2>/dev/null; then
  echo "安装新增组件（窗口界面 / PPTX 解析）…"
  pip_install "${INDEX[@]}" -e "source[ui]" || exit 1
fi
# 文字识别组件（可选）：装不上不影响使用，只是扫描件不能识别
if [ "$(uname)" = "Darwin" ] && ! "$PY" -c "import Vision, Quartz" 2>/dev/null && [ ! -f data/.ocr-install-failed ]; then
  echo "安装文字识别组件（扫描版 PDF）…"
  pip_install "${INDEX[@]}" -e "source[ocr]" || { mkdir -p data; touch data/.ocr-install-failed; echo "文字识别组件没装上，跳过（删除 data/.ocr-install-failed 可重试）"; }
fi
"$PY" -c "import lkos, webview, pptx" 2>/dev/null
