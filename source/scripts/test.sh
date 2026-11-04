#!/usr/bin/env bash
# 运行全部测试，所有临时文件都放在项目根目录的 .pytest_tmp/ 下（不进 Git）。
# 用法：source/scripts/test.sh            单元测试 + 功能测试 + 覆盖率
#       source/scripts/test.sh --live     额外运行真实 Ollama 模型测试
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

RUN_DIR="$ROOT/.pytest_tmp/run-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN_DIR/tmp"
export TMPDIR="$RUN_DIR/tmp"
export PYTHONDONTWRITEBYTECODE=1
export COVERAGE_FILE="$RUN_DIR/.coverage"

if [[ "${1:-}" == "--live" ]]; then
  export LKOS_LIVE=1
fi

PY="${PYTHON:-.venv/bin/python}"
PY="$(cd "$(dirname "$PY")" && pwd)/$(basename "$PY")"
cd "$ROOT/source"
"$PY" -m pytest tests --basetemp="$RUN_DIR/basetemp" \
  --cov=lkos --cov-report=term-missing:skip-covered -q
