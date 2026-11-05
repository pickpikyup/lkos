#!/bin/bash
# 双击运行：用 data/eval/m1-questions.yaml（61 题）评测当前版本，结果写入 data/eval/<时间>/，并和上一次对比。
# 用的是应用正在用的索引（data/），不需要重建；可以开着应用，但评测期间别提问。大约 15–20 分钟，请接着电源。
cd "$(dirname "$0")/../.."   # 项目根目录
STAMP="$(date +%Y%m%d-%H%M)"
OUT="data/eval/$STAMP"
mkdir -p "$OUT"
export PYTHONDONTWRITEBYTECODE=1 COLUMNS=170
L=".venv/bin/lkos"
Q="${1:-data/eval/m1-questions.yaml}"
[ -f "$Q" ] || { echo "找不到题目文件：$Q"; read -r -n 1 -s -p "按任意键退出…"; exit 1; }
source/scripts/install_ui.sh >/dev/null 2>&1   # 确保程序登记在 source/ 下

{
  echo "== 环境 =="
  source/scripts/ollama_tune.sh --check
  sysctl -n vm.swapusage
  $L status | grep -E "文件|文本块|问答模型|上下文"

  echo; echo "== 1. 检索评测：flat（新默认） =="
  $L eval "$Q" --strategy flat --out "$OUT/retrieval-flat.json"
  echo; echo "== 2. 检索评测：doc_first（对照） =="
  $L eval "$Q" --strategy doc_first --out "$OUT/retrieval-docfirst.json"

  echo; echo "== 3. 问答评测：flat + 新提示词（允许部分回答） =="
  $L eval "$Q" --with-answer --strategy flat --out "$OUT/answers-flat.json"
  sysctl -n vm.swapusage

  echo; echo "== 对比 =="
  .venv/bin/python - "$OUT" <<'PY'
import json, sys
from pathlib import Path
out = Path(sys.argv[1])
rows = [("上次 v0.2（800 字 + flat，旧提示词）", "data/eval/m1-20261103/answers-v2-flat.json"),
        ("上次 M1（按语言切块 + doc_first，旧提示词）", "data/eval/m1-20261103/answers-m1-docfirst.json"),
        ("本次（按语言切块 + flat，新提示词）", str(out / "answers-flat.json"))]
def nf(d):  # 与当前规则一致地重算“文档对但没找到”
    n = 0
    for r in d["results"]:
        a = r.get("answer") or ""
        short_nf = (not r.get("cited")) and len(a) <= 60 and any(
            m in a.lower() for m in ("没有找到", "未找到", "没有相关", "no relevant", "not found", "aucune information"))
        n += bool(r.get("hit") and short_nf)
    return n
print(f"{'':<34}{'文件命中':>8}{'段落命中':>8}{'答对':>8}{'有出处':>8}{'答没找到':>8}{'首字中位':>10}")
for name, p in rows:
    try:
        d = json.loads(Path(p).read_text("utf-8"))
    except OSError:
        print(f"{name:<34}（没有结果文件）"); continue
    pct = lambda v: "-" if v is None else f"{v:.0%}"
    ttft = d.get("ttft_median_ms")
    print(f"{name:<34}{pct(d['hit_rate']):>8}{pct(d.get('chunk_hit_rate')):>8}{pct(d.get('answer_accuracy')):>8}"
          f"{pct(d.get('citation_rate')):>8}{nf(d):>8}{('-' if ttft is None else f'{ttft/1000:.1f}s'):>10}")
PY
  echo; echo "结果在 $OUT/ 。把这个窗口的内容或 $OUT/run.log 发给我分析。"
} 2>&1 | tee "$OUT/run.log"
read -r -n 1 -s -p "按任意键退出…"
