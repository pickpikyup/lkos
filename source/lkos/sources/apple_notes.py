"""Mac 备忘录（Apple Notes）：把备忘录导出成 Markdown，再像普通文件夹一样索引。

为什么不直接读备忘录的数据库：NoteStore.sqlite 受系统“完全磁盘访问权限”保护，格式也是未公开的压缩 protobuf。
这里用系统自带的 osascript（JavaScript for Automation）向“备忘录”应用要数据：第一次会弹出
“LKOS 想要控制备忘录”的授权，同意即可；拒绝后可在“系统设置 → 隐私与安全性 → 自动化”里打开。

- 导出到 <数据目录>/apple-notes/<账户>/<文件夹>/<标题>.md（data/ 不进 Git，内容不离开本机）；
- 只重写有变化的备忘录，文件修改时间设为备忘录的修改时间；删掉的备忘录对应的文件一并删除；
- 加了锁的备忘录、“最近删除”里的备忘录不导出；附件（图片、PDF 等）暂不导出，只保留正文文字；
- manifest.json 记下文件与备忘录 ID 的对应，“打开”时直接在备忘录应用里显示那一条。
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from lkos.errors import LkosError
from lkos.logs import get_logger

log = get_logger("notes")

DIR_NAME = "apple-notes"
MANIFEST = "manifest.json"
TRASH_NAMES = {"Recently Deleted", "最近删除", "最近刪除", "Suppressions récentes"}
OSA_TIMEOUT = 600

# 一次取回所有备忘录（按文件夹批量取属性，比逐条快得多）
JXA = r"""
ObjC.import('Foundation');
function run(argv) {
const app = Application('Notes');
const out = [];
function pathOf(f) {
  const names = [];
  let cur = f;
  for (let i = 0; i < 8; i++) {
    try { names.unshift(cur.name()); } catch (e) { break; }
    let parent;
    try { parent = cur.container(); } catch (e) { break; }
    let kind = '';
    try { kind = ObjectSpecifier.classOf(parent); } catch (e) { break; }
    if (kind === 'account') { try { names.unshift(parent.name()); } catch (e) {} break; }
    if (kind !== 'folder') break;
    cur = parent;
  }
  return names;
}
for (const f of app.folders()) {
  const path = pathOf(f);
  const n = f.notes;
  let ids;
  try { ids = n.id(); } catch (e) { continue; }
  if (!ids.length) continue;
  const names = n.name(), mods = n.modificationDate(), created = n.creationDate();
  let locked = [];
  try { locked = n.passwordProtected(); } catch (e) { locked = ids.map(() => false); }
  let bodies = [];
  try { bodies = n.body(); } catch (e) {
    // 有加锁的备忘录时整批取正文可能失败：改为逐条取，取不到的留空
    bodies = ids.map((_, i) => { if (locked[i]) return ''; try { return n[i].body(); } catch (e2) { return ''; } });
  }
  for (let i = 0; i < ids.length; i++) {
    let m = '', c = '';
    try { m = mods[i].toISOString(); } catch (e) {}
    try { c = created[i].toISOString(); } catch (e) {}
    out.push({ id: String(ids[i]), name: String(names[i] || ''), path: path, modified: m, created: c,
               locked: !!locked[i], body: locked[i] ? '' : String(bodies[i] || '') });
  }
}
// 结果直接以 UTF-8 写进文件：不经过 osascript 的标准输出（中文、表情符号在那里可能被转码弄坏）
const s = JSON.stringify(out);
if (argv.length && $(s).writeToFileAtomicallyEncodingError(argv[0], true, $.NSUTF8StringEncoding, $())) return 'ok:' + out.length;
return s;
}
"""


def available() -> bool:
    return sys.platform == "darwin" and shutil.which("osascript") is not None


def export_dir(home: Path) -> Path:
    return Path(home) / DIR_NAME


def is_notes_dir(path: Path, home: Path) -> bool:
    try:
        return Path(path).resolve() == export_dir(home).resolve()
    except OSError:
        return False


def fetch_notes() -> list[dict]:
    """向备忘录应用要全部备忘录（只在 macOS 上可用）。"""
    if not available():
        raise LkosError("只有 macOS 上才能读取备忘录")
    env = {**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
    with tempfile.TemporaryDirectory(prefix="lkos-notes-") as tmp:
        out_file = Path(tmp) / "notes.json"
        try:
            p = subprocess.run(["osascript", "-l", "JavaScript", "-e", JXA, str(out_file)], capture_output=True,
                               timeout=OSA_TIMEOUT, env=env)
        except subprocess.TimeoutExpired as e:
            raise LkosError("读取备忘录超时（备忘录很多时第一次会比较慢），请稍后再试") from e
        raw = out_file.read_bytes() if out_file.exists() else b""
    stdout = (p.stdout or b"").decode("utf-8", errors="replace").strip()
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", errors="replace").strip()
        if "-1743" in err or "Not authorized" in err or "不允许" in err:
            raise LkosError("没有权限读取备忘录：请在“系统设置 → 隐私与安全性 → 自动化”里，允许 LKOS（或终端）控制“备忘录”")
        if "-600" in err or "isn't running" in err:
            raise LkosError("备忘录应用没有响应，请打开一次“备忘录”后再试")
        raise LkosError(f"读取备忘录失败：{err[:200] or p.returncode}")
    return parse_output(raw.decode("utf-8", errors="replace") if raw else stdout)


def parse_output(text: str) -> list[dict]:
    """解析 JXA 返回的 JSON；容忍控制字符、坏的代理字符等，解析不了时在日志里写明位置。"""
    text = (text or "").strip().lstrip("\ufeff")
    if not text or text.startswith("ok:"):
        return []
    try:
        data = json.loads(text, strict=False)
    except ValueError as e:
        pos = getattr(e, "pos", None)
        log.warning("备忘录返回的内容无法解析：%s（共 %d 字符，开头：%r）", e, len(text), text[:60])
        if pos is not None:
            log.warning("出错位置附近：%r", text[max(0, pos - 40): pos + 40])
        raise LkosError("读取备忘录失败：返回的内容无法解析（详情已写入日志，可以把日志发给开发者）") from e
    if not isinstance(data, list):
        return []
    return [n for n in data if isinstance(n, dict)]


# ---------------- HTML → Markdown ----------------
class _ToMarkdown(HTMLParser):
    BLOCK = {"div", "p", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.lists: list[str] = []
        self.ol_n: list[int] = []
        self.in_pre = False

    def _nl(self):
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in ("ul", "ol"):
            self._nl()
            self.lists.append(tag)
            self.ol_n.append(0)
        elif tag == "li":
            self._nl()
            depth = max(0, len(self.lists) - 1)
            if self.lists and self.lists[-1] == "ol":
                self.ol_n[-1] += 1
                self.parts.append("  " * depth + f"{self.ol_n[-1]}. ")
            else:
                self.parts.append("  " * depth + "- ")
        elif tag in ("h1", "h2", "h3", "h4"):
            self._nl()
            self.parts.append("#" * min(int(tag[1]) + 1, 4) + " ")   # 备忘录标题已占用一级标题
        elif tag == "br":
            self.parts.append("\n")
        elif tag in ("b", "strong"):
            self.parts.append("**")
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag == "pre":
            self._nl()
            self.in_pre = True
        elif tag in self.BLOCK:
            self._nl()

    def handle_endtag(self, tag):
        if tag in ("ul", "ol") and self.lists:
            self.lists.pop()
            self.ol_n.pop()
            self._nl()
        elif tag in ("b", "strong"):
            self.parts.append("**")
        elif tag == "pre":
            self.in_pre = False
            self._nl()
        elif tag in self.BLOCK:
            self._nl()

    def handle_data(self, data):
        self.parts.append(data if self.in_pre else re.sub(r"[ \t\r\n]+", " ", data))


def html_to_markdown(body: str) -> str:
    p = _ToMarkdown()
    p.feed(body or "")
    p.close()
    text = html.unescape("".join(p.parts)).replace("\xa0", " ")
    text = re.sub(r"\*\*\s*\*\*", "", text)
    lines = [ln.rstrip() for ln in text.split("\n")]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out


# ---------------- 导出 ----------------
def _safe(name: str, limit: int = 80) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name or "").strip(" .")
    return (name[:limit].rstrip(" .") or "无标题")


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def load_manifest(home: Path) -> dict:
    try:
        return json.loads((export_dir(home) / MANIFEST).read_text("utf-8"))
    except (OSError, ValueError):
        return {"notes": {}}


def note_id_for(home: Path, path: str) -> str | None:
    rel = os.path.relpath(Path(path).resolve(), export_dir(home).resolve())
    for nid, info in load_manifest(home).get("notes", {}).items():
        if info.get("file") == rel:
            return nid
    return None


_sync_lock = threading.Lock()


def synced_recently(home: Path, seconds: float = 600) -> bool:
    """最近 seconds 秒内同步过（如刚点了“添加 Mac 备忘录”）：索引时不用马上再读一遍。"""
    at = load_manifest(home).get("synced_at")
    try:
        return at is not None and (datetime.now() - datetime.fromisoformat(at)).total_seconds() < seconds
    except ValueError:
        return False


def sync(home: Path, fetch: Callable[[], list[dict]] | None = None) -> dict:
    """把备忘录导出到 <数据目录>/apple-notes。返回 {written, removed, skipped, total}。同一时间只做一次。"""
    with _sync_lock:
        return _sync(home, fetch)


def _sync(home: Path, fetch: Callable[[], list[dict]] | None = None) -> dict:
    notes = (fetch or fetch_notes)()
    root = export_dir(home)
    root.mkdir(parents=True, exist_ok=True)
    old = load_manifest(home).get("notes", {})
    new: dict[str, dict] = {}
    used: set[str] = set()
    old_files = {v.get("file") for v in old.values()}
    written = skipped = 0
    # 先给已有的备忘录保留原来的文件名，避免标题相同的两条互换文件
    ordered = sorted(notes, key=lambda n: (n.get("id") not in old, n.get("created") or ""))
    for n in ordered:
        nid = str(n.get("id") or "")
        folder = [str(x) for x in n.get("path") or []]
        if not nid or n.get("locked") or (folder and folder[-1] in TRASH_NAMES):
            skipped += 1
            continue
        title = (n.get("name") or "").strip() or "无标题"
        sub = Path(*[_safe(x, 60) for x in folder]) if folder else Path("备忘录")
        prev = old.get(nid, {}).get("file")
        same_name = prev and re.fullmatch(re.escape(_safe(title)) + r"( \(\d+\))?\.md", Path(prev).name)
        rel = prev if same_name and Path(prev).parent == sub and prev not in used else None   # 标题、文件夹都没变：沿用文件名
        if rel is None:
            base, k = _safe(title), 1
            rel = str(sub / f"{base}.md")
            while rel in used or (rel in old_files and rel != prev):
                k += 1
                rel = str(sub / f"{base} ({k}).md")
        used.add(rel)
        mtime = _ts(n.get("modified") or "")
        target = root / rel
        new[nid] = {"file": rel, "modified": n.get("modified"), "title": title, "folder": folder}
        if prev == rel and old[nid].get("modified") == n.get("modified") and target.exists():
            continue
        body = html_to_markdown(n.get("body") or "")
        first = body.split("\n", 1)[0].strip().lstrip("#").strip()
        if first == title:                       # 备忘录的正文第一行通常就是标题
            body = body.split("\n", 1)[1].strip() if "\n" in body else ""
        meta = " · ".join(x for x in ["备忘录", " / ".join(folder), (n.get("modified") or "")[:10]] if x)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {title}\n\n{body}\n\n<!-- {meta} -->\n", "utf-8")
        if mtime:
            os.utime(target, (mtime, mtime))
        if prev and prev != rel and (root / prev).exists():
            (root / prev).unlink()
        written += 1
    removed = 0
    keep = {v["file"] for v in new.values()}
    for nid, info in old.items():
        f = info.get("file")
        if nid not in new and f and f not in keep and (root / f).exists():
            (root / f).unlink()
            removed += 1
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            d.rmdir()                            # 只删空文件夹
        except OSError:
            pass
    (root / MANIFEST).write_text(json.dumps({"notes": new, "synced_at": datetime.now().isoformat(timespec="seconds")},
                                            ensure_ascii=False, indent=1), "utf-8")
    return {"written": written, "removed": removed, "skipped": skipped, "total": len(new)}


def show_note(note_id: str) -> str | None:
    """在备忘录应用里显示这一条；返回错误信息或 None。"""
    if not available():
        return "只有 macOS 上才能打开备忘录"
    script = f"const a = Application('Notes'); a.activate(); a.notes.byId({json.dumps(note_id)}).show();"
    try:
        subprocess.Popen(["osascript", "-l", "JavaScript", "-e", script], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as e:
        return f"无法打开备忘录：{e}"
    return None
