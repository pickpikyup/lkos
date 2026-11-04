#!/usr/bin/env bash
# 在 MacBook（Apple Silicon）上一键搭建并验证 POC：
#   1. 检查 Ollama 是否安装、是否在运行，缺少的模型自动下载
#   2. 在项目目录内创建 Python 环境（Python 本体与缓存都放在 .cache/ 下）
#   3. 运行单元测试 + 功能测试，再运行真实模型测试
#   4. 用样本文档跑一遍 init → index → ask
#   5. 运行内存基准测试
#
# 用法：
#   scripts/setup_mac.sh                     全部步骤
#   scripts/setup_mac.sh --docs ~/文档目录    第 4 步改为索引你自己的文件夹（默认最多 300 个文件）
#   scripts/setup_mac.sh --skip-benchmark    跳过第 5 步
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

CHAT_MODEL="qwen3:4b-instruct"
EMBED_MODEL="bge-m3"
DOCS=""
BENCH=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --docs) DOCS="$2"; shift 2 ;;
    --skip-benchmark) BENCH=0; shift ;;
    *) echo "未知参数：$1"; exit 64 ;;
  esac
done

step() { printf "\n\033[1;34m== %s ==\033[0m\n" "$1"; }
die()  { printf "\033[1;31m错误：%s\033[0m\n" "$1"; exit 1; }

# 所有缓存留在项目目录内
export UV_CACHE_DIR="$ROOT/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$ROOT/.cache/python"
export LKOS_HOME="$ROOT/data"
export PYTHONDONTWRITEBYTECODE=1

step "1/5 检查环境与 Ollama"
[[ "$(uname -s)" == "Darwin" ]] || die "这个脚本只用于 macOS。"
[[ "$(uname -m)" == "arm64" ]] || echo "提示：不是 Apple Silicon，性能数据不具参考意义。"
echo "内存：$(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 )) GB"

command -v ollama >/dev/null || die "未安装 Ollama。请从 https://ollama.com/download 下载安装，或执行 brew install ollama。"
if ! curl -sf -m 3 http://127.0.0.1:11434/api/tags >/dev/null; then
  echo "Ollama 未运行，尝试启动…"
  open -a Ollama 2>/dev/null || (nohup ollama serve >/dev/null 2>&1 &)
  for _ in $(seq 1 30); do
    curl -sf -m 2 http://127.0.0.1:11434/api/tags >/dev/null && break
    sleep 1
  done
  curl -sf -m 3 http://127.0.0.1:11434/api/tags >/dev/null || die "Ollama 启动失败，请手动打开 Ollama 应用后重试。"
fi
echo "Ollama 已运行：$(ollama --version 2>/dev/null | head -1)"
for m in "$CHAT_MODEL" "$EMBED_MODEL"; do
  if ollama list | awk 'NR>1{print $1}' | grep -qx -e "$m" -e "$m:latest"; then
    echo "模型 $m 已存在"
  else
    echo "下载模型 $m …（qwen3:4b-instruct 约 2.5 GB，bge-m3 约 1.2 GB）"
    ollama pull "$m"
  fi
done

step "2/5 创建 Python 环境（.venv）"
# 首次需要下载约 300 MB 依赖（PyMuPDF、lingua 等）。国内网络慢时可指定镜像，例如：
#   PIP_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple scripts/setup_mac.sh
if [[ -n "${PIP_MIRROR:-}" ]]; then
  export UV_INDEX_URL="$PIP_MIRROR" PIP_INDEX_URL="$PIP_MIRROR"
  echo "使用镜像：$PIP_MIRROR"
fi
echo "安装依赖（首次约 300 MB，下面会显示下载进度）…"
if command -v uv >/dev/null; then
  [[ -x .venv/bin/python ]] || uv venv --python 3.11 .venv
  uv pip install --python .venv/bin/python -e ".[test,ui]"
else
  PY=""
  for c in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11; do
    [[ -x "$c" ]] && PY="$c" && break
  done
  [[ -n "$PY" ]] || die "需要 uv 或 Homebrew 的 Python 3.11+（macOS 自带 Python 不能加载 SQLite 扩展）。请执行：brew install uv"
  [[ -x .venv/bin/python ]] || "$PY" -m venv .venv
  .venv/bin/pip install -e ".[test,ui]"
fi
.venv/bin/python -c "import sqlite3, sqlite_vec; c = sqlite3.connect(':memory:'); c.enable_load_extension(True); sqlite_vec.load(c); print('sqlite-vec', c.execute('select vec_version()').fetchone()[0])" \
  || die "当前 Python 不能加载 sqlite-vec。"

# 提交前自动拦下私人数据（文档、数据库、日志）
git config core.hooksPath scripts/git-hooks 2>/dev/null || true

step "3/5 运行测试（单元 + 功能 + 真实模型）"
TEST_STATUS="通过"
scripts/test.sh --live || TEST_STATUS="有失败（见上方 FAILURES），继续执行演示与基准"

step "4/5 端到端演示"
LKOS=".venv/bin/lkos"
$LKOS init
if [[ -n "$DOCS" ]]; then
  $LKOS index "$DOCS" --limit 300
  echo "已索引你的文件夹。现在可以执行：$LKOS ask \"你的问题\""
else
  .venv/bin/python scripts/make_samples.py "$LKOS_HOME/samples"
  $LKOS index "$LKOS_HOME/samples"
  $LKOS ask "项目二期的预算上限是多少？"
  $LKOS ask "What is the retention policy for logs?"
  $LKOS ask "Où se trouve la machine à café ?"
  $LKOS eval examples/questions.example.yaml --with-answer
fi
$LKOS status

if [[ "$BENCH" == 1 ]]; then
  step "5/5 内存基准（建议同时打开 10 个浏览器标签页，并观察活动监视器的内存压力）"
  .venv/bin/python scripts/benchmark_memory.py --rounds 10
fi

printf "\n\033[1;32m完成。\033[0m测试：%s。数据在 %s，可以删除后重来。\n" "$TEST_STATUS" "$LKOS_HOME"
