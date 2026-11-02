"""生成演示用样本文档（中、英、法；PDF、DOCX、Markdown）。

运行：python scripts/make_samples.py data/samples
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.samples import build_corpus  # noqa: E402

if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "data/samples")
    build_corpus(target)
    for p in (target / ".hidden.md", target / "~$项目计划书.docx", target / "notes.txt"):
        p.unlink(missing_ok=True)  # 这几个文件只用于测试“应被忽略”的规则
    print(f"样本文档已生成：{target}")
