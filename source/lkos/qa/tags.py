"""自动标签：读每份文件的开头（文件名 + 前约 1200 字），让模型给出 3–5 个主题标签。

- 优先沿用资料库里已有的标签，避免同一主题出现“预算”“预算管理”“经费”好几种写法；
- 一次很短的调用（输出不超过 48 个 token），在后台闲时进行，问答时让路；
- 用户手动加的标签不会被覆盖；文件内容变化后重新生成。
"""

from __future__ import annotations

import re
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

from lkos.errors import Cancelled
from lkos.logs import get_logger
from lkos.qa.service import answer_lang
from lkos.types import FileRecord

TASK = "knowledge_qa"
HEAD_CHARS = 1200
TAG_TOKENS = 48
MAX_TAGS = 5
log = get_logger("tags")

PROMPTS = {
    "zh": "下面是文件《{name}》的开头：\n{text}\n\n请给这份文件 3–5 个主题标签，每个 2–8 个字（名词或名词短语），"
          "用顿号“、”分隔，只输出标签。{vocab}",
    "en": "Here is the beginning of the file \"{name}\":\n{text}\n\nGive 3–5 short topic tags (1–3 words each), "
          "separated by commas. Output only the tags.{vocab}",
}
VOCAB = {"zh": "能用的话优先用这些已有标签：{tags}。", "en": " Prefer these existing tags when they fit: {tags}."}
_SPLIT = re.compile(r"\s*[、,，;；/|\n]\s*")


def normalize_tag(t: str) -> str:
    t = re.sub(r"^\s*(?:[-*•#]|\d+[.、)）])\s*", "", t)
    t = t.strip(" 　\"'“”‘’《》「」[]【】()（）。.:：")
    return t if 1 <= len(t) <= 16 and not t.isdigit() else ""


def parse_tags(text: str, limit: int = MAX_TAGS) -> list[str]:
    text = re.sub(r"^\s*(?:标签|tags?)\s*[:：]", "", text.strip(), flags=re.I)
    out = []
    for raw in _SPLIT.split(text):
        t = normalize_tag(raw)
        if t and t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out[:limit]


class Tagger:
    def __init__(self, cfg, repo, gateway, marker=None):
        self.cfg = cfg
        self.repo = repo
        self.gateway = gateway
        self.marker = marker

    def _head(self, f: FileRecord) -> str:
        text = ""
        for c in self.repo.chunks_of_file(f.path):
            text += c.text.strip() + "\n"
            if len(text) >= HEAD_CHARS:
                break
        return text[:HEAD_CHARS].strip()

    def tag_file(self, f: FileRecord, vocab: list[str] | None = None) -> list[str]:
        head = self._head(f)
        name = Path(f.path).name
        if not head:
            self.repo.set_model_tags(f.id, [])
            return []
        key = "zh" if answer_lang(head[:300]) == "zh" else "en"
        hint = VOCAB[key].format(tags="、".join(vocab[:30]) if key == "zh" else ", ".join(vocab[:30])) if vocab else ""
        prompt = PROMPTS[key].format(name=name, text=head, vocab=hint)
        with (self.marker.active() if self.marker else nullcontext()):
            result, _ = self.gateway.chat(TASK, [{"role": "user", "content": prompt}], max_tokens=TAG_TOKENS,
                                          log_as="tags")
        tags = parse_tags(result.text)
        self.repo.set_model_tags(f.id, tags)
        return tags

    def run(self, source_id: int | None = None, limit: int | None = None, gate=None,
            on_progress: Callable[[int, int, str], None] | None = None,
            cancelled: Callable[[], bool] | None = None) -> int:
        """给还没有标签的文件生成标签，返回处理的文件数。gate：闲时闸门（每个文件前检查）。"""
        files = self.repo.files_to_tag(source_id, limit)
        done = 0
        for i, f in enumerate(files, start=1):
            if cancelled and cancelled():
                raise Cancelled("已停止生成标签")
            if gate is not None:
                gate.wait()
            vocab = [t["tag"] for t in self.repo.all_tags(40)]
            tags = self.tag_file(f, vocab)
            log.debug("标签 %s：%s", Path(f.path).name, "、".join(tags) or "（无）")
            done += 1
            if on_progress:
                on_progress(i, len(files), f.path)
        return done
