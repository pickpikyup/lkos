"""在测试运行时生成样本文件，避免在仓库中保存二进制文件。"""

from __future__ import annotations

from pathlib import Path

import docx
try:
    import pymupdf as fitz
except ImportError:  # 旧版本只有 fitz 包名
    import fitz
from docx.oxml.ns import qn
from docx.oxml import OxmlElement


def make_pdf(path: Path, pages: list[str], encrypt_password: str | None = None) -> Path:
    """每个元素一页；中文使用 PyMuPDF 内置的 china-s 字体。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        rect = fitz.Rect(50, 50, 545, 790)
        page.insert_textbox(rect, text, fontsize=11, fontname="china-s")
    if encrypt_password:
        doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=encrypt_password,
                 owner_pw=encrypt_password + "-owner")
    else:
        doc.save(str(path))
    doc.close()
    return path


def make_docx(path: Path, items: list[tuple[str, str]], wps_style_names: bool = False) -> Path:
    """items: (kind, text)。kind 为 h1 / h2 / p / table（text 用 '|' 分隔单元格，';' 分隔行）。

    wps_style_names=True 时，用名为“标题 1”的自定义样式 + 大纲级别，模拟 WPS 保存的文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    d = docx.Document()
    if wps_style_names:
        for level in (1, 2):
            style = d.styles.add_style(f"标题 {level}", 1)  # 1 = WD_STYLE_TYPE.PARAGRAPH
            ppr = style.element.get_or_add_pPr()
            lvl = OxmlElement("w:outlineLvl")
            lvl.set(qn("w:val"), str(level - 1))
            ppr.append(lvl)
    for kind, text in items:
        if kind in ("h1", "h2"):
            level = int(kind[1])
            if wps_style_names:
                d.add_paragraph(text, style=f"标题 {level}")
            else:
                d.add_heading(text, level=level)
        elif kind == "p":
            d.add_paragraph(text)
        elif kind == "table":
            rows = [r.split("|") for r in text.split(";")]
            table = d.add_table(rows=len(rows), cols=len(rows[0]))
            for i, row in enumerate(rows):
                for j, cell in enumerate(row):
                    table.cell(i, j).text = cell
        else:
            raise ValueError(kind)
    d.save(str(path))
    return path


ZH_PLAN = [
    ("h1", "第三章 预算"),
    ("p", "本项目分两期实施，一期已于去年完成验收。"),
    ("h2", "3.2 二期"),
    ("p", "二期预算上限为一百二十万元，其中硬件采购不超过四十万元。"),
    ("p", "预算由财务部统一管理，超支需要提前报批。"),
    ("table", "项目|金额;硬件|四十万;软件|八十万"),
]

ZH_MINUTES_PAGES = [
    "评审会议纪要\n会议时间：三月十二日。参会人员：项目组全体成员。",
    "评审结论：确认二期预算上限，同意按计划启动招标。\n会议同时要求每月汇报进度。",
]

EN_POLICY_MD = """---
title: Security policy
---
# Security Policy

## Log retention

Application logs are retained for ninety days and then deleted automatically.

Audit logs are kept for one year in encrypted storage.

## Passwords

```
# this is a code comment, not a heading
password_min_length = 12
```

Passwords must be rotated every six months.
"""

FR_GUIDE_MD = """# Guide d'accueil

## Horaires

Les bureaux sont ouverts du lundi au vendredi, de neuf heures à dix-huit heures.

## Café

La machine à café se trouve au deuxième étage, près de la salle de réunion.
"""


def build_corpus(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    make_docx(root / "项目计划书.docx", ZH_PLAN)
    make_pdf(root / "评审会议纪要.pdf", ZH_MINUTES_PAGES)
    (root / "security-policy.md").write_text(EN_POLICY_MD, encoding="utf-8")
    sub = root / "fr"
    sub.mkdir(exist_ok=True)
    (sub / "guide-accueil.md").write_text(FR_GUIDE_MD, encoding="utf-8")
    # 应被忽略的文件
    (root / ".hidden.md").write_text("# hidden\n\nshould be ignored", encoding="utf-8")
    (root / "~$项目计划书.docx").write_bytes(b"lock file")
    (root / "notes.xyz").write_text("unsupported extension", encoding="utf-8")
    return root


def make_pptx(path: Path, slides: list[dict]) -> Path:
    """slides: [{"title": ..., "body": [行, ...], "table": [[单元格, ...], ...], "notes": ...}]"""
    from pptx import Presentation
    from pptx.util import Inches

    path.parent.mkdir(parents=True, exist_ok=True)
    prs = Presentation()
    for s in slides:
        slide = prs.slides.add_slide(prs.slide_layouts[1])   # 标题 + 内容
        slide.shapes.title.text = s.get("title", "")
        body = slide.placeholders[1].text_frame
        lines = s.get("body", [])
        if lines:
            body.text = lines[0]
            for line in lines[1:]:
                body.add_paragraph().text = line
        if s.get("table"):
            rows = s["table"]
            shape = slide.shapes.add_table(len(rows), len(rows[0]), Inches(1), Inches(5), Inches(6), Inches(1))
            for r, row in enumerate(rows):
                for c, val in enumerate(row):
                    shape.table.cell(r, c).text = val
        if s.get("notes"):
            slide.notes_slide.notes_text_frame.text = s["notes"]
    prs.save(str(path))
    return path
