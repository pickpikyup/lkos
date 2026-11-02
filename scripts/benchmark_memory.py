"""实施第 1 步：内存与速度基准测试（POC 设计文档第 12 节）。

同时使用聊天模型和 Embedding 模型，连续提问 N 次，每次记录可用内存、交换区和首 token 时间。
建议同时打开 10 个浏览器标签页，模拟日常使用。

运行：python scripts/benchmark_memory.py --rounds 10
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lkos.config import load_config  # noqa: E402
from lkos.gateway.base import ChatOptions  # noqa: E402
from lkos.gateway.gateway import memory_snapshot  # noqa: E402
from lkos.gateway.ollama import OllamaProvider  # noqa: E402

CONTEXT = ("二期预算上限为一百二十万元，其中硬件采购不超过四十万元。预算由财务部统一管理，超支需要提前报批。" * 40)
QUESTIONS = [
    "二期预算上限是多少？请引用 [1]。",
    "硬件采购的上限是多少？",
    "超支需要怎么处理？",
    "What is the budget cap for phase two?",
    "Qui gère le budget ?",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--chat-model", default="qwen3:4b-instruct")
    ap.add_argument("--embed-model", default="bge-m3")
    args = ap.parse_args()

    cfg = load_config(None)
    p = OllamaProvider("local", base_url=cfg.providers["local"].options["base_url"])
    for m in (args.chat_model, args.embed_model):
        chk = p.check_model(m)
        if not chk.ok:
            print(f"{m}：{chk.message}")
            return 2

    mem0, swap0 = memory_snapshot()
    print(f"开始前：可用内存 {mem0} MB，交换区 {swap0} MB")
    rows = []
    for i in range(args.rounds):
        q = QUESTIONS[i % len(QUESTIONS)]
        t = time.perf_counter()
        # 每轮在资料开头加上不同内容，避免 Ollama 复用上一轮的提示缓存，让首 token 时间更接近真实提问
        ctx = f"（第 {i + 1} 轮资料，编号 {i * 7919 % 1000}）" + CONTEXT
        p.embed(args.embed_model, [q] + [ctx[:800]] * 4)
        emb_ms = int((time.perf_counter() - t) * 1000)
        res = p.chat(args.chat_model, [
            {"role": "system", "content": "只根据资料回答，并用 [编号] 标注出处。"},
            {"role": "user", "content": f"资料：\n[1]\n{ctx}\n\n问题：{q}"},
        ], ChatOptions(max_tokens=256, num_ctx=8192, think=False))
        mem, swap = memory_snapshot()
        rows.append((emb_ms, res.ttft_ms, res.latency_ms, res.input_tokens, mem, swap))
        print(f"#{i + 1:02d} embed {emb_ms:5d} ms · 首 token {res.ttft_ms or 0:6d} ms（其中加载 {res.load_ms or 0} ms）"
              f" · 总 {res.latency_ms:6d} ms"
              f" · 输入 {res.input_tokens} tokens · 可用 {mem} MB · 交换区 {swap} MB")

    ttft = [r[1] for r in rows[1:] if r[1] is not None] or [r[1] for r in rows if r[1] is not None]
    swaps = [r[5] for r in rows if r[5] is not None]
    print("\n结论")
    print(f"  首 token 中位数（不含第 1 轮冷启动）：{statistics.median(ttft) / 1000:.2f}s（目标 ≤ 5s）")
    print(f"  第 1 轮首 token：{(rows[0][1] or 0) / 1000:.2f}s（含模型加载）")
    if swaps and swap0 is not None:
        growth = swaps[-1] - swap0
        print(f"  交换区增长：{growth} MB（目标：不持续增长）")
    print(f"  最低可用内存：{min(r[4] for r in rows if r[4] is not None)} MB")
    print("  同时观察 Activity Monitor 的“内存压力”曲线是否长时间为红色。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
