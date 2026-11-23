"""界面里回答的 Markdown 渲染（app.js 的 MD）：用 Node 执行同一段代码检查输出；没装 Node 时跳过。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "lkos" / "ui" / "static" / "app.js"
NODE = shutil.which("node")

RUNNER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const a = src.indexOf("/*MD-BEGIN*/"), b = src.indexOf("/*MD-END*/");
const MD = new Function(src.slice(a, b) + "\nreturn MD;")();
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const cite = (nums) => nums.map((n) => `<b data-ref="${n}">${n}</b>`).join("");
const out = {};
for (const [k, v] of Object.entries(cases)) out[k] = MD.render(v, k.startsWith("plain") ? {} : { cite });
process.stdout.write(JSON.stringify(out));
"""


def render(cases: dict) -> dict:
    r = subprocess.run([NODE, "-e", RUNNER, str(APP_JS)], input=json.dumps(cases), capture_output=True, text=True,
                       timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.skipif(NODE is None, reason="没有安装 Node.js")
def test_markdown_render():
    out = render({
        "table": "**公开信息**\n| 项目 | 信息 |\n|---|:--:|\n| 芯片 | 昇腾 960 |\n| 时间 | **2027 年 Q4** [1] |",
        "table_streaming": "| 项目 | 信息 |\n|---|",
        "table_blank_rows": "| a | b |\n|---|---|\n\n| 1 | 2 |\n\n| 3 | 4 |\n\n后文",
        "lists": "1. 第一\n2. 第二\n   - 子项\n3. 第三\n\n- [x] 完成",
        "code": "```python\nprint('<b>')\n```",
        "code_open": "```js\nconst a = 1;",
        "quote": "> 引用 **粗**",
        "head": "### 预算\n上限 [1, 2]",
        "links": "见 [年报](https://example.com/a?x=1&y=2) 和 https://n.example.org/b。",
        "xss": "<script>alert(1)</script> [点](javascript:alert(1)) <img onerror=x>",
        "inline": "*斜* ~~删~~ `a|b` 2*3*4 snake_case_name",
        "plain": "对话里的 [1] 保持文字",
    })
    t = out["table"]
    assert t.startswith("<p><strong>公开信息</strong></p><div class=\"md-table\"><table>")
    assert "<th>项目</th>" in t and 'style="text-align:center"' in t and '<td style="text-align:center">昇腾 960</td>' in t
    assert "<strong>2027 年 Q4</strong>" in t and '<b data-ref="1">1</b>' in t
    assert "<table>" in out["table_streaming"]                                  # 流式输出：分隔行一到就显示为表格
    assert out["table_blank_rows"].count("<tr>") == 3 and out["table_blank_rows"].endswith("<p>后文</p>")
    assert out["lists"].startswith("<ol><li>第一</li><li>第二<ul><li>子项</li></ul></li><li>第三</li></ol>")
    assert "md-task done" in out["lists"]
    assert out["code"] == '<pre class="md-code" data-lang="python"><code>print(\'&lt;b&gt;\')</code></pre>'
    assert "<pre" in out["code_open"]                                           # 没收尾的代码块也按代码显示
    assert out["quote"] == "<blockquote><p>引用 <strong>粗</strong></p></blockquote>"
    assert 'md-h3' in out["head"] and '<b data-ref="2">2</b>' in out["head"]
    assert 'data-open-url="https://example.com/a?x=1&amp;y=2"' in out["links"] and 'data-open-url="https://n.example.org/b"' in out["links"]
    x = out["xss"]                                                              # 模型输出里的 HTML 一律当文字
    assert "<script" not in x and "<img" not in x and "&lt;script&gt;" in x and "data-open-url" not in x
    i = out["inline"]
    assert "<em>斜</em>" in i and "<del>删</del>" in i and "<code>a|b</code>" in i and "2*3*4" in i and "snake_case_name" in i
    assert out["plain"] == "<p>对话里的 [1] 保持文字</p>"
