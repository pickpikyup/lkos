"""联网搜索：搜索 → 取网页正文 → 选出和问题最相关的段落 → 变成 RetrievedChunk（file_type = "web"，路径为网址）。

搜索服务（界面“设置 → 联网搜索”）：
- bing：必应（cn.bing.com，不需要密钥，国内可直接访问）；
- duckduckgo：DuckDuckGo 网页版（不需要密钥，需要能访问境外网站）；
- tavily：Tavily 搜索 API（需要密钥，直接返回网页正文，结果最干净）。

开关随时可改：每次要联网时才读一次开关（enabled()），深度分析检索到一半时打开 / 关闭也会对还没做的检索生效。
离线模式下一律不联网。所有失败都不让回答中断：记下原因、只用本机资料继续。
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

from lkos.logs import get_logger
from lkos.types import Location, RetrievedChunk

log = get_logger("web")

PROVIDERS = {"bing": "必应 Bing", "duckduckgo": "DuckDuckGo", "tavily": "Tavily（需要密钥）"}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
MAX_PAGE_BYTES = 2 * 1024 * 1024


class WebSearchError(Exception):
    pass


@dataclass
class WebResult:
    title: str
    url: str
    snippet: str = ""
    content: str = ""       # 网页正文（Tavily 直接给；必应 / DuckDuckGo 另外去取）


@dataclass
class WebRun:
    """一次回答里的联网情况（存进问答记录，界面显示“联网搜索了几次、找到几个网页”）。"""
    queries: list[str] = field(default_factory=list)
    pages: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: str = ""       # 没联网的原因（开关关着 / 离线模式）

    def to_dict(self) -> dict:
        d = {"queries": self.queries, "pages": self.pages}
        if self.errors:
            d["errors"] = self.errors[:3]
        if self.skipped and not self.queries:
            d["skipped"] = self.skipped
        return d


# ---------- 解析 ----------
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v 　]+")


def strip_tags(s: str) -> str:
    return _WS.sub(" ", html.unescape(_TAG.sub("", s or ""))).strip()


def _bing_url(href: str) -> str:
    """必应结果的链接常包成 bing.com/ck/a?...&u=a1<base64>：还原成原网址。"""
    href = html.unescape(href)
    if "bing.com/ck/a" in href:
        u = (parse_qs(urlparse(href).query).get("u") or [""])[0]
        if u.startswith("a1"):
            raw = u[2:]
            try:
                return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
            except ValueError:
                return href
    return href


def parse_bing(page: str, k: int) -> list[WebResult]:
    out = []
    for block in page.split('<li class="b_algo"')[1:]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        url = _bing_url(m.group(1))
        if not url.startswith("http"):
            continue
        sm = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        out.append(WebResult(title=strip_tags(m.group(2)), url=url, snippet=strip_tags(sm.group(1)) if sm else ""))
        if len(out) >= k:
            break
    return out


def _ddg_url(href: str) -> str:
    href = html.unescape(href)
    if "duckduckgo.com/l/" in href:
        u = (parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query).get("uddg") or [""])[0]
        return unquote(u) if u else href
    return href


def parse_duckduckgo(page: str, k: int) -> list[WebResult]:
    out = []
    for block in re.split(r'<div[^>]+class="[^"]*\bresult\b', page)[1:]:
        m = re.search(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S) or \
            re.search(r'<a[^>]+href="([^"]+)"[^>]+class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        url = _ddg_url(m.group(1))
        if not url.startswith("http") or "duckduckgo.com/y.js" in url:    # 跳过广告
            continue
        sm = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div)>', block, re.S)
        out.append(WebResult(title=strip_tags(m.group(2)), url=url, snippet=strip_tags(sm.group(1)) if sm else ""))
        if len(out) >= k:
            break
    return out


_DROP = re.compile(r"<(script|style|noscript|svg|nav|footer|header|aside|form|template)\b.*?</\1>", re.S | re.I)
_BLOCK = re.compile(r"</?(p|div|br|li|tr|h[1-6]|section|article|table|ul|ol|blockquote|pre)\b[^>]*>", re.I)


def page_text(raw: str) -> tuple[str, list[str]]:
    """网页 HTML → (标题, 正文段落)。去掉脚本、导航、页脚等，只留像正文的段落。"""
    tm = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    title = strip_tags(tm.group(1)) if tm else ""
    body = _DROP.sub(" ", re.sub(r"<!--.*?-->", " ", raw, flags=re.S))
    body = _BLOCK.sub("\n", body)
    paras = []
    for line in html.unescape(_TAG.sub(" ", body)).splitlines():
        line = _WS.sub(" ", line).strip()
        han = len(re.findall(r"[一-鿿]", line))
        if han >= 15 or len(line) >= 60:      # 太短的多半是菜单、按钮、版权行
            paras.append(line)
    return title, paras


def query_terms(query: str) -> list[str]:
    terms = [t.lower() for t in re.findall(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9.\-]+", query)]
    for run in re.findall(r"[一-鿿]+", query):
        terms += [run[i:i + 2] for i in range(len(run) - 1)]     # 中文按两字切分：“战略更新”也能命中“更新了战略”
    return [t for t in dict.fromkeys(terms) if len(t) >= 2]


def best_passages(paras: list[str], query: str, max_chars: int) -> str:
    """按命中的关键词数挑段落（保持原顺序），凑够 max_chars 为止；一个都没命中就取开头。"""
    if not paras:
        return ""
    terms = query_terms(query)
    scored = sorted(range(len(paras)), key=lambda i: -sum(paras[i].lower().count(t) for t in terms))
    if not terms or not any(t in paras[scored[0]].lower() for t in terms):
        scored = list(range(len(paras)))
    best = scored[0]
    if len(paras[best]) >= max_chars:            # 最相关的一段就放不下：只取它
        return paras[best][:max_chars]
    picked, used = [best], len(paras[best])
    for i in scored[1:]:                          # 其余段落按相关程度补充，放不下的跳过（不截断最相关的段落）
        if used + len(paras[i]) + 1 <= max_chars:
            picked.append(i)
            used += len(paras[i]) + 1
    return "\n".join(paras[i] for i in sorted(picked))


def domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def web_chunk(r: WebResult, text: str, lang: str = "") -> RetrievedChunk:
    cid = -int(hashlib.md5(f"web\n{r.url}".encode()).hexdigest()[:12], 16)
    return RetrievedChunk(chunk_id=cid, file_path=r.url, file_type="web", text=text, lang=lang,
                          location=Location(heading_path=r.title or domain(r.url)), score=0.0)


# ---------- 搜索 ----------
class WebSearcher:
    def __init__(self, settings: Callable[[], dict] | None = None, offline: Callable[[], bool] | None = None,
                 transport: httpx.BaseTransport | None = None, timeout_s: float = 10.0):
        self.settings = settings or (lambda: {})
        self.offline = offline or (lambda: False)
        self._transport = transport
        self._timeout_s = timeout_s
        self._client: httpx.Client | None = None
        self.run = WebRun()

    @property
    def client(self) -> httpx.Client:
        """第一次联网时才创建（建立 TLS 环境要几十毫秒，不联网就不花这个时间）。"""
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(self._timeout_s, connect=6.0), transport=self._transport,
                                        trust_env=True, follow_redirects=True, headers={"User-Agent": UA})
        return self._client

    def begin(self) -> WebRun:
        self.run = WebRun()
        return self.run

    def enabled(self) -> bool:
        """每次要联网时才读开关：用户随时可以打开 / 关闭。"""
        if not self.settings().get("web_search"):
            self.run.skipped = "联网搜索已关闭"
            return False
        if self.offline():
            self.run.skipped = "离线模式已开启，没有联网"
            return False
        return True

    @property
    def provider(self) -> str:
        p = self.settings().get("web_provider") or "bing"
        return p if p in PROVIDERS else "bing"

    def search(self, query: str, k: int = 5) -> list[WebResult]:
        query = re.sub(r"\s+", " ", query or "").strip()[:200]
        if not query:
            return []
        p = self.provider
        try:
            if p == "tavily":
                return self._tavily(query, k)
            if p == "duckduckgo":
                r = self.client.post("https://html.duckduckgo.com/html/", data={"q": query})
                r.raise_for_status()
                res = parse_duckduckgo(r.text, k)
            else:
                r = self.client.get(f"https://cn.bing.com/search?q={quote_plus(query)}&ensearch=0")
                r.raise_for_status()
                res = parse_bing(r.text, k)
        except httpx.HTTPStatusError as e:
            raise WebSearchError(f"{PROVIDERS[p]} 返回 HTTP {e.response.status_code}") from e
        except httpx.HTTPError as e:
            raise WebSearchError(f"连不上 {PROVIDERS[p]}（{type(e).__name__}），请检查网络或代理") from e
        if not res and len(r.text) > 0:
            raise WebSearchError(f"{PROVIDERS[p]} 没有返回可识别的结果（可能被要求验证），可以在设置里换一个搜索服务")
        return res

    def _tavily(self, query: str, k: int) -> list[WebResult]:
        key = (self.settings().get("web_key") or "").strip()
        if not key:
            raise WebSearchError("用 Tavily 需要先在“设置 → 联网搜索”里填写密钥")
        try:
            r = self.client.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {key}"},
                                 json={"query": query, "max_results": k, "search_depth": "basic"})
        except httpx.HTTPError as e:
            raise WebSearchError(f"连不上 Tavily（{type(e).__name__}），请检查网络或代理") from e
        if r.status_code in (401, 403):
            raise WebSearchError("Tavily 密钥无效")
        if r.status_code >= 400:
            raise WebSearchError(f"Tavily 返回 HTTP {r.status_code}")
        return [WebResult(title=x.get("title") or "", url=x.get("url") or "", content=x.get("content") or "")
                for x in (r.json().get("results") or []) if x.get("url")][:k]

    def _fetch(self, r: WebResult) -> tuple[str, list[str]]:
        try:
            with self.client.stream("GET", r.url) as resp:
                ctype = resp.headers.get("content-type", "")
                if resp.status_code >= 400 or ("html" not in ctype and "text" not in ctype):
                    return "", []
                buf = b""
                for part in resp.iter_bytes():
                    buf += part
                    if len(buf) > MAX_PAGE_BYTES:
                        break
                enc = resp.encoding or "utf-8"
            return page_text(buf.decode(enc, "replace"))
        except (httpx.HTTPError, LookupError):
            return "", []

    def chunks(self, query: str, k: int = 4, fetch: int = 3, max_chars: int = 1500,
               cancelled: Callable[[], bool] | None = None) -> list[RetrievedChunk]:
        """搜索 query，取前 fetch 个网页的正文，选出最相关的段落；失败时返回空列表并记下原因。"""
        if not self.enabled():
            return []
        t0 = time.perf_counter()
        try:
            results = self.search(query, k)
        except WebSearchError as e:
            log.warning("联网搜索失败（%s）：%s", query, e)
            self.run.errors.append(str(e))
            return []
        self.run.queries.append(query)
        if cancelled and cancelled():
            return []
        need = [r for r in results[:fetch] if not r.content]
        pages = {}
        if need:
            with ThreadPoolExecutor(max_workers=min(4, len(need))) as ex:
                pages = dict(zip((r.url for r in need), ex.map(self._fetch, need)))
        out = []
        for i, r in enumerate(results):
            if r.content:
                text = best_passages([p for p in r.content.splitlines() if p.strip()], query, max_chars)
            else:
                title, paras = pages.get(r.url, ("", []))
                r.title = r.title or title
                text = best_passages(paras, query, max_chars) if paras else ""
            if r.snippet and r.snippet not in text:
                text = (r.snippet + ("\n" + text if text else ""))[:max_chars]
            if text.strip():
                out.append(web_chunk(r, text.strip()))
        self.run.pages += len(out)
        log.info("联网搜索（%s）：%s → %d 个网页（%d ms）：%s", PROVIDERS[self.provider], query, len(out),
                 int((time.perf_counter() - t0) * 1000), "、".join(domain(c.file_path) for c in out) or "无")
        return out

    def check(self) -> str:
        """设置页“测试”按钮。"""
        p = self.provider
        try:
            res = self.search("Local Knowledge OS 测试", 3)
        except WebSearchError as e:
            return f"不可用：{e}"
        return f"{PROVIDERS[p]} 可用，返回 {len(res)} 条结果"


# ---------- 搜索词 ----------
# 问题里“怎么做”的指令词（查找、核实、联网……）和问句词：搜进去只会搜到无关网页（例如“互联网核实”搜到 ChatGPT 官网）
_ZH_DROP = sorted("""现在 然后 并且 同时 另外 请 帮我 帮忙 麻烦 一下 给我 结合 根据 利用 通过 本地 本机 资料库 知识库 资料 文档
互联网 网上 网络 联网 在线 网页 信息 查找 查询 搜索 搜一下 找找 找一下 查一下 查查 核实 核对 验证 确认 检索 整理 总结 列出 给出
输出 事实 客观 不编造 保持 真实 详细 你说的 这些 那些 都对 好的 我想 想知道 看看 告诉我 了解 是多少 是不是 有没有 是否 是什么 有哪些 是哪些 有什么 怎么样 怎样 如何 为什么 多少 吗 呢 吧""".split(), key=len, reverse=True)
_LATIN_DROP = set("""internet online web search verify verification verified check checking local information info find
look lookup please now then and the a an of to in on for with recherche vérification vérifier vérifiée informations
information locale local en ligne""".split())


def clean_query(text: str) -> str:
    """去掉指令词、问句词和标点，留下要搜的内容；英文词去重（不区分大小写）。"""
    s = re.sub(r"[，,。.！!？?；;：:、（）()\[\]【】“”\"'《》<>/\\|]+", " ", text or "")
    for w in _ZH_DROP:
        s = s.replace(w, " ")
    out, seen = [], set()
    for tok in s.split():
        low = tok.lower()
        if low in _LATIN_DROP or low in seen:
            continue
        seen.add(low)
        out.append(tok)
    return " ".join(out)[:120]


def _entity(kw: str) -> str:
    """关键词里的主体（第一个词；第二个词带数字时一起，如“Ascend 960”）。"""
    toks = kw.split()
    if not toks:
        return ""
    return " ".join(toks[:2]) if len(toks) > 1 and re.search(r"\d", toks[1]) else toks[0]


def web_queries(question: str, keywords: dict, aspects: list[str], lang: str, online: bool) -> list[str]:
    """深度分析的联网搜索词：关键词（按回答语言；中文回答再加一条英文）+（在线模型）前两个方面。
    方面里没有主体词时才在前面补上（避免“Ascend Ascend 960 …”这类重复）。"""
    main = clean_query(keywords.get("zh" if lang == "zh" else "en") or "") or clean_query(question)
    out = [main] if main else []
    if online:
        en = clean_query(keywords.get("en") or "")
        if lang == "zh" and en:
            out.append(en)
        ent = _entity(main)
        for a in aspects[:2]:
            a = clean_query(a)
            if not a or a == clean_query(question):
                continue
            out.append(a if not ent or ent.lower() in a.lower() else f"{ent} {a}")
    seen, uniq = set(), []
    for q in out:
        if q.lower() not in seen:
            seen.add(q.lower())
            uniq.append(q)
    return uniq[:4] if online else uniq[:1]


def interleave(*groups):
    """轮流取各组的第 1、第 2……个，去重（按 chunk_id）。"""
    out, seen = [], set()
    for rank in range(max((len(g) for g in groups), default=0)):
        for g in groups:
            if rank < len(g) and g[rank].chunk_id not in seen:
                seen.add(g[rank].chunk_id)
                out.append(g[rank])
    return out
