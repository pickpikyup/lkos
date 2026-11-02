#!/usr/bin/env bash
# 运行全部测试，所有临时文件都放在项目目录内的 .pytest_tmp/ 下。
# 用法：scripts/test.sh            单元测试 + 功能测试 + 覆盖率
#       scripts/test.sh --live     额外运行真实 Ollama 模型测试
set -euo pipefail
cd "$(dirname "$0")/.."

RUN_DIR=".pytest_tmp/run-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN_DIR"
export TMPDIR="$PWD/$RUN_DIR/tmp"
mkdir -p "$TMPDIR"
export PYTHONDONTWRITEBYTECODE=1

if [[ "${1:-}" == "--live" ]]; then
  export LKOS_LIVE=1
fi

PY="${PYTHON:-.venv/bin/python}"
"$PY" -m pytest tests --basetemp="$RUN_DIR/basetemp" \
  --cov=lkos --cov-report=term-missing:skip-covered -q
