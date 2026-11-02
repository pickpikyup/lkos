#!/bin/bash
# 双击运行：用 data/test-docs 中的真实文档测试索引、检索与问答，输出写入 logs/。
# 使用独立的数据目录 data/realdocs-home，不与样本演示的索引混在一起。
cd "$(dirname "$0")"
mkdir -p logs
LOG="logs/realdocs-$(date +%Y%m%d-%H%M%S).log"
export LKOS_HOME="$PWD/data/realdocs-home" PYTHONDONTWRITEBYTECODE=1 COLUMNS=160
L=".venv/bin/lkos"
Q="${1:-data/real-questions.yaml}"
{
  echo "== 内存（开始前） =="; vm_stat | head -6; sysctl vm.swapusage
  echo; echo "== 1. 索引 data/test-docs =="
  /usr/bin/time -l $L index data/test-docs 2>&1 | grep -vE "^\s+[0-9]+\s+(page|block|signals|voluntary|involuntary|messages|instructions|cycles)" 
  echo; echo "== 2. 检索评测：按问题语言过滤（默认） =="; $L eval "$Q" --k 5
  echo; echo "== 3. 检索评测：不过滤语言 =="; $L eval "$Q" --k 5 --lang all
  echo; echo "== 4. 问答与引用评测（默认设置） =="; $L eval "$Q" --k 5 --with-answer
  echo; echo "== 5. 问答示例 =="
  $L ask "部署 DeepSeek V3 的最小配置需要几台 Atlas 800I A2？" --no-stream
  $L ask "How many racks are planned for the Ooredoo AI data center?" --no-stream
  $L ask "Ooredoo AI 数据中心规划了多少个机柜？" --no-stream
  echo; echo "== 6. 状态 =="; $L status --failed
  echo; echo "== 内存（结束后） =="; vm_stat | head -6; sysctl vm.swapusage
  echo; echo "完成。"
} 2>&1 | tee "$LOG"
read -r -n 1 -s -p "按任意键退出…"
