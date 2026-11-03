#!/bin/bash
# 双击运行：M1 实测（按语言切块 + 先选文档再选段落），与 v0.2 对比。输出写入 logs/。
#   v0.2 基线：沿用 data/realdocs-v2 的索引（800 字切块），用 --strategy flat 评测
#   M1：新建 data/realdocs-m1，按新的默认配置（中文 500 字 / 英文 1,200 字符）重建索引，用 doc_first 评测
# 题目：data/m1-questions.yaml（61 题，带含答案原文与答案，可用第一个参数换成别的题目文件）
cd "$(dirname "$0")"
mkdir -p logs
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="logs/m1-$STAMP.log"
OUT="logs/m1-$STAMP"
mkdir -p "$OUT"
export PYTHONDONTWRITEBYTECODE=1 COLUMNS=170
L=".venv/bin/lkos"
Q="${1:-data/m1-questions.yaml}"
V2="$PWD/data/realdocs-v2"
M1="$PWD/data/realdocs-m1"

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

  echo; echo "== 1. M1 索引：data/realdocs-m1（按语言切块） =="
  LKOS_HOME="$M1" $L init
  LKOS_HOME="$M1" $L index data/test-docs
  LKOS_HOME="$M1" $L status
  snap "索引后"

  echo; echo "== 2. 检索评测（不生成，几秒钟） =="
  echo "-- 2a. v0.2 基线：800 字切块 + flat --"
  LKOS_HOME="$V2" $L eval "$Q" --strategy flat --out "$OUT/retrieval-v2-flat.json"
  echo "-- 2b. 只换选段：800 字切块 + doc_first --"
  LKOS_HOME="$V2" $L eval "$Q" --strategy doc_first --out "$OUT/retrieval-v2-docfirst.json"
  echo "-- 2c. 只换切块：按语言切块 + flat --"
  LKOS_HOME="$M1" $L eval "$Q" --strategy flat --out "$OUT/retrieval-m1-flat.json"
  echo "-- 2d. M1：按语言切块 + doc_first --"
  LKOS_HOME="$M1" $L eval "$Q" --strategy doc_first --out "$OUT/retrieval-m1-docfirst.json"

  echo; echo "== 3. 问答评测：v0.2 基线（800 字 + flat，qwen3:4b-instruct） =="
  LKOS_HOME="$V2" $L eval "$Q" --with-answer --strategy flat --out "$OUT/answers-v2-flat.json"
  snap "v0.2 问答后"

  echo; echo "== 4. 问答评测：M1（按语言切块 + doc_first + 补邻） =="
  LKOS_HOME="$M1" $L eval "$Q" --with-answer --strategy doc_first --out "$OUT/answers-m1-docfirst.json"
  snap "M1 问答后"

  echo; echo "每题结果：$OUT/"
  echo "完成。"
} 2>&1 | tee "$LOG"
read -r -n 1 -s -p "按任意键退出…"
