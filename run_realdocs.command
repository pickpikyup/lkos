#!/bin/bash
# 双击运行：用 data/test-docs 中的真实文档测试 v0.2（二值 + int8 向量、Embedding 缓存、语言加权、4K 上下文）。
# 使用独立的数据目录 data/realdocs-v2（v0.1 的索引格式不兼容），输出写入 logs/。
# 分阶段记录交换区与 ollama ps，并把每题回答原文保存为 JSON，便于对比 4B 与 1.7B。
cd "$(dirname "$0")"
mkdir -p logs
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="logs/realdocs-v2-$STAMP.log"
OUT="logs/realdocs-v2-$STAMP"
mkdir -p "$OUT"
export LKOS_HOME="$PWD/data/realdocs-v2" PYTHONDONTWRITEBYTECODE=1 COLUMNS=160
L=".venv/bin/lkos"
Q="${1:-data/real-questions.yaml}"

snap() {
  echo "-- 内存 [$1] $(date +%H:%M:%S) --"
  sysctl -n vm.swapusage
  memory_pressure 2>/dev/null | grep -E "System-wide memory free percentage" || true
  ollama ps 2>/dev/null || true
}

{
  echo "== 0. 环境 =="
  scripts/ollama_tune.sh --check
  snap "开始前"

  echo; echo "== 1. 索引 data/test-docs =="
  $L index data/test-docs
  snap "索引后"

  echo; echo "== 2. 检索评测：soft（默认，同语言加权） =="
  $L eval "$Q" --out "$OUT/retrieval-soft.json"
  echo; echo "== 3. 检索评测：strict（只检索同语言，对照 v0.1） =="
  $L eval "$Q" --lang-mode strict --out "$OUT/retrieval-strict.json"

  echo; echo "== 4. 问答评测：qwen3:4b-instruct =="
  $L eval "$Q" --with-answer --out "$OUT/answers-4b.json"
  snap "4B 问答后"

  echo; echo "== 5. 问答评测：qwen3:1.7b（对比） =="
  if ollama list | awk 'NR>1{print $1}' | grep -qx "qwen3:1.7b" || ollama pull qwen3:1.7b; then
    ollama stop qwen3:4b-instruct 2>/dev/null || true
    $L eval "$Q" --with-answer --model local/qwen3:1.7b --out "$OUT/answers-1.7b.json"
    snap "1.7B 问答后"
  else
    echo "qwen3:1.7b 下载失败，跳过对比（如开着 Shadowrocket，需要让 *.r2.cloudflarestorage.com 直连）"
  fi

  echo; echo "== 6. 状态 =="
  $L status --failed
  snap "结束"
  echo; echo "每题结果：$OUT/"
  echo "完成。"
} 2>&1 | tee "$LOG"
read -r -n 1 -s -p "按任意键退出…"
