/* Local Knowledge OS 应用界面。通过 window.pywebview.api 调用 Python（lkos/ui/bridge.py），
   Python 通过 window.lkos.onEvent(事件) 推送问答进度与索引进度。 */
(function () {
  "use strict";

  const S = {
    view: "qa", mode: "ask", models: [], model: "", history: [], sources: [], index: { state: "idle" },
    paused: false, cur: null, sel: null, lib: null, confirmRemove: null, problemsCount: 0, libEdit: null,
    settings: { dev_mode: false },
    scope: { source_ids: [], file_types: [], days: 0, path: null, tags: [] }, scopeOpen: false, followUp: null, tags: [],
    log: { tab: "run", after: 0, lines: [], level: "DEBUG", filter: "", file: "", warn: 0, calls: [] },
  };
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (n) => Number(n || 0).toLocaleString("zh-CN");
  const sec = (ms) => (ms == null ? "" : (ms / 1000).toFixed(1) + " 秒");

  function api(name, ...args) {
    const a = window.pywebview && window.pywebview.api;
    if (!a || !a[name]) return Promise.reject(new Error("应用还没准备好"));
    return a[name](...args);
  }

  let toastTimer = null;
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
  }

  // ---------------- 推送事件 ----------------
  window.lkos = {
    onEvent(e) {
      const c = S.cur;
      if (e.type === "index") {
        S.index = e;
        renderStatus();
        if (S.view === "lib") renderLib();
        return;
      }
      if (e.type === "library_changed") { refreshLibrary(); return; }
      if (!c || c.qid !== e.qid) return;
      if (e.type === "progress") {
        c.progress = e;
        if (c.phase === "retrieving") c.phase = "reading";
        renderQA();
      } else if (e.type === "sources") {
        c.sources = e.sources; c.retrievalMs = e.ms; c.phase = "reading"; c.readingSince = Date.now();
        S.sel = e.sources.length ? 1 : null;
        renderQA(); renderAside();
      } else if (e.type === "token") {
        if (c.phase !== "streaming") { c.phase = "streaming"; renderQA(); }
        c.text += e.text;
        const el = $("answer-live");
        if (el) el.innerHTML = answerHTML(c.text, true);
        keepBottom();
      } else if (e.type === "done") {
        S.cur = { phase: "done", record: e.record, question: e.record.question, kind: e.record.kind };
        const cited = e.record.cited || [];
        S.sel = cited.length ? cited[0] : (e.record.context.length ? 1 : null);
        loadHistory();
        setGenerating(false);
        renderQA(); renderAside();
      } else if (e.type === "stopped") {
        c.phase = "stopped"; setGenerating(false); renderQA();
      } else if (e.type === "error") {
        c.phase = "error"; c.error = e.message; setGenerating(false); renderQA();
      }
    },
  };

  function keepBottom() {
    const m = $("main-scroll");
    if (m.scrollHeight - m.scrollTop - m.clientHeight < 160) m.scrollTop = m.scrollHeight;
  }

  // ---------------- 启动 ----------------
  let booted = false;
  async function boot() {
    if (booted) return;
    booted = true;
    try {
      const b = await api("boot");
      S.models = b.models; S.model = b.model; S.history = b.history; S.sources = b.sources; S.index = b.index;
      S.stats = b.stats; S.settings = b.settings || S.settings;
    } catch (err) {
      S.bootError = String(err && err.message || err);
    }
    applyDevMode();
    renderSide(); renderStatus(); renderQA(); renderAside(); renderScope();
    $("q").focus();
  }
  window.addEventListener("pywebviewready", boot);

  async function loadHistory() {
    try { S.history = await api("history"); } catch (e) { /* 忽略 */ }
    renderSide();
  }

  async function refreshLibrary() {
    try {
      const lib = await api("library");
      S.lib = lib; S.sources = lib.sources; S.stats = lib.stats; S.index = lib.index; S.paused = lib.paused;
      S.problemsCount = lib.problems.length;
    } catch (e) { /* 忽略 */ }
    renderSide(); renderStatus();
    if (S.view === "lib") renderLib();
  }

  // ---------------- 左栏 ----------------
  function dayLabel(iso) {
    const d = new Date(iso);
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const diff = Math.floor((today - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
    return diff <= 0 ? "今天" : diff === 1 ? "昨天" : diff < 7 ? "近 7 天" : "更早";
  }

  function renderSide() {
    const curId = S.cur && S.cur.record && S.cur.record.id;
    let html = "", last = null;
    if (!S.history.length) html = '<div class="side-label">问答记录</div><div class="side-empty">提过的问题会出现在这里</div>';
    for (const h of S.history.slice(0, 80)) {
      const g = dayLabel(h.asked_at);
      if (g !== last) { html += `<div class="side-label">${g}</div>`; last = g; }
      html += `<button type="button" class="side-item${h.id === curId && S.view === "qa" ? " active" : ""}" data-hid="${h.id}" title="${esc(h.question)}"><span class="t">${esc(h.question)}</span></button>`;
    }
    $("history").innerHTML = html;
    let f = "";
    for (const s of S.sources) {
      const name = s.path.split("/").filter(Boolean).pop() || s.path;
      f += `<button type="button" class="side-item${S.view === "lib" ? " active" : ""}" data-lib="1" title="${esc(s.path)}">
        <svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"><path d="M2 4.5h4l1.5 1.5H14v6.5H2z"/></svg>
        <span class="t">${esc(name)}</span><span class="n">${fmt(s.files)}</span></button>`;
    }
    if (!S.sources.length) f = '<div class="side-empty">还没有添加文件夹</div>';
    f += `<button type="button" class="side-item" data-lib="1"><span class="t">管理资料库…</span></button>`;
    $("folders").innerHTML = f;
  }

  const REASON = {
    qa: "正在问答，答完自动继续", manual: "已手动暂停", busy: "你正在使用电脑，空闲后自动继续",
    battery: "正在用电池，接通电源后继续", daemon: "后台服务正在索引，等它做完当前文件",
  };

  function renderStatus() {
    const card = $("status-card"), i = S.index || {};
    const name = i.folder ? i.folder.split("/").filter(Boolean).pop() : "";
    let cls = "idle", h = "索引已是最新", d = "按各文件夹的设置自动更新";
    if (i.state === "running" && i.kind === "tags") {
      cls = "running"; h = "正在生成标签"; d = `${name} · ${fmt(i.done)} / ${fmt(i.total)} 个文件`;
    } else if (i.state === "running") {
      cls = "running"; h = "后台索引中"; d = `${name} · ${fmt(i.done)} / ${fmt(i.total)} 个文件`;
    } else if (i.state === "paused") {
      cls = "paused"; h = "后台索引已暂停"; d = REASON[i.reason] || "";
      if (i.total) d += ` · ${Math.floor((i.done / i.total) * 100)}%`;
    } else if (i.state === "error") {
      cls = "error"; h = "索引出错"; d = i.reason || "";
    } else if (!S.sources.length) {
      cls = "empty"; h = "还没有资料"; d = "点这里添加文件夹";
    } else if (i.queued) {
      cls = "running"; h = "等待索引"; d = `${i.queued} 个文件夹排队中`;
    }
    card.className = "status-card " + cls;
    $("status-h").textContent = h;
    $("status-d").textContent = d;
  }

  // ---------------- 范围（文件夹 / 类型 / 时间 / 单个文件） ----------------
  const TYPES = [["pdf", "PDF", ["pdf"]], ["docx", "Word", ["docx", "doc"]], ["pptx", "PowerPoint", ["pptx", "ppt"]], ["md", "Markdown", ["md"]]];
  const DAYS = [[0, "不限"], [7, "近 7 天"], [30, "近 30 天"], [365, "近一年"]];
  const baseName = (p) => String(p || "").split("/").filter(Boolean).pop() || p;

  function scopeDict() {
    const sc = S.scope, d = {};
    if (sc.path) return { path: sc.path };
    if (sc.source_ids.length) d.source_ids = sc.source_ids.slice();
    if (sc.file_types.length) d.file_types = TYPES.filter((t) => sc.file_types.includes(t[0])).flatMap((t) => t[2]);
    if (sc.days) d.since = Date.now() / 1000 - sc.days * 86400;
    if (sc.tags.length) d.tags = sc.tags.slice();
    return Object.keys(d).length ? d : null;
  }

  function scopeText(sc) {
    if (!sc) return "全部资料";
    if (sc.path) return `《${baseName(sc.path)}》`;
    const parts = [];
    if (sc.source_ids && sc.source_ids.length) {
      const names = S.sources.filter((s) => sc.source_ids.includes(s.id)).map((s) => baseName(s.path));
      parts.push(names.length > 2 ? `${names.length} 个文件夹` : names.join("、") || "所选文件夹");
    }
    if (sc.file_types && sc.file_types.length) {
      parts.push(TYPES.filter((t) => t[2].some((x) => sc.file_types.includes(x))).map((t) => t[1]).join("/"));
    }
    if (sc.since) {
      const days = Math.round((Date.now() / 1000 - sc.since) / 86400);
      const hit = DAYS.find((x) => x[0] && Math.abs(x[0] - days) <= 1);
      parts.push(hit ? hit[1] : `近 ${days} 天`);
    }
    if (sc.tags && sc.tags.length) parts.push("标签 " + sc.tags.join("、"));
    return parts.join(" · ") || "全部资料";
  }

  function renderScope() {
    const sc = S.scope, d = scopeDict();
    $("scope-label").textContent = "范围：" + scopeText(d);
    $("scope-btn").classList.toggle("on", !!d);
    $("scope-btn").setAttribute("aria-expanded", String(S.scopeOpen));
    const pop = $("scope-pop");
    pop.hidden = !S.scopeOpen;
    if (!S.scopeOpen) return;
    let h = "";
    if (sc.path) {
      h += `<div><h3>只用这一份文件</h3></div><div class="file"><span title="${esc(sc.path)}">${esc(baseName(sc.path))}</span>
        <button type="button" data-scope-clear-file="1" aria-label="取消，改回按文件夹和类型">×</button></div>`;
    } else {
      h += `<div><h3>文件夹</h3><div class="opts">${S.sources.length ? S.sources.map((s) => `<label><input type="checkbox" data-scope-src="${s.id}"${sc.source_ids.includes(s.id) ? " checked" : ""}><span class="p" title="${esc(s.path)}">${esc(baseName(s.path))}</span></label>`).join("") : '<span class="hint">还没有添加文件夹</span>'}</div></div>
        <div><h3>文件类型</h3><div class="opts inline">${TYPES.map((t) => `<label><input type="checkbox" data-scope-type="${t[0]}"${sc.file_types.includes(t[0]) ? " checked" : ""}>${t[1]}</label>`).join("")}</div></div>
        ${S.tags.length || sc.tags.length ? `<div><h3>标签</h3><div class="opts inline">${[...new Set([...sc.tags, ...S.tags.slice(0, 24).map((t) => t.tag)])].map((t) => `<label><input type="checkbox" data-scope-tag="${esc(t)}"${sc.tags.includes(t) ? " checked" : ""}>${esc(t)}</label>`).join("")}</div></div>` : ""}
        <div><h3>修改时间</h3><div class="opts inline">${DAYS.map((x) => `<label><input type="radio" name="scope-days" data-scope-days="${x[0]}"${sc.days === x[0] ? " checked" : ""}>${x[1]}</label>`).join("")}</div></div>
        <div class="hint">不勾选表示不限。想只问一份文件：在右侧出处里点“只问这份文件”。</div>`;
    }
    h += `<div class="foot"><button type="button" class="btn small" data-scope-reset="1">恢复全部资料</button><button type="button" class="btn small primary" data-scope-close="1">完成</button></div>`;
    pop.innerHTML = h;
  }

  function setScopeFile(path) {
    S.scope = { source_ids: [], file_types: [], days: 0, path, tags: [] };
    renderScope();
  }

  // ---------------- 问答区 ----------------
  function modelBanner() {
    const bad = S.models.filter((m) => !m.ok);
    if (S.bootError) return `<div class="banner" role="alert">启动出错：${esc(S.bootError)}</div>`;
    if (!bad.length) return "";
    return `<div class="banner" role="alert"><div><strong>模型没有就绪</strong>：${bad.map((m) => esc(m.ref)).join("、")}。<br>请先打开 Ollama 应用；如果缺模型，在终端运行 <code>ollama pull 模型名</code>，然后重新打开本应用。</div></div>`;
  }

  function answerHTML(text, streaming) {
    let h = esc(text).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    h = h.replace(/^#{2,4}\s*(.+)(?:\n|$)/gm, '<span class="ah">$1</span>');
    h = h.replace(/\[(\d+(?:\s*[,，、]\s*\d+)*)\]/g, (_, nums) =>
      nums.split(/\s*[,，、]\s*/).map((n) => `<button type="button" class="ref${Number(n) === S.sel ? " on" : ""}" data-ref="${n}" aria-label="出处 ${n}">${n}</button>`).join(""));
    return h + (streaming ? '<span class="caret" aria-hidden="true"></span>' : "");
  }

  function foundPills(sources) {
    const files = [];
    for (const s of sources) if (!files.includes(s.file)) files.push(s.file);
    return `<div class="found"><span>找到 ${files.length} 份资料</span>${files.slice(0, 3).map((f) => `<span class="pill" title="${esc(f)}">${esc(f)}</span>`).join("")}</div>`;
  }

  const KIND_LABEL = { qa: "你问", search: "你搜索", summary: "全文总结", analysis: "深度分析", extract: "信息提取" };
  function questionBlock(c) {
    const r = c.record, kind = c.phase === "search" ? "search" : (c.kind || (r && r.kind) || (/^总结：《/.test(c.question) ? "summary" : "qa"));
    const ex = (r && r.extra) || {};
    let label = KIND_LABEL[kind] || "你问";
    if (kind === "qa" && (ex.follow_up || c.followUp)) label = "你追问";
    const title = String(c.question).replace(/^(分析|提取)：/, "");
    const sc = r ? (ex.path && kind === "extract" ? { path: ex.path } : (ex.scope && Object.keys(ex.scope).length ? ex.scope : null)) : c.scope;
    const scopeNote = sc && kind !== "summary" ? `<span class="q-scope"> · 范围：${esc(scopeText(sc))}</span>` : "";
    return `<div><div class="q-label">${label}${scopeNote}</div><h1 class="q-title">${esc(title)}</h1></div>`;
  }

  function step(cls, label, sub) {
    return `<li class="${cls}"><span class="ic" aria-hidden="true">${cls === "done" ? checkSvg() : ""}</span>
      <span class="label">${label}</span>${sub ? `<span class="sub">${sub}</span>` : ""}</li>`;
  }
  const SKELETON = '<div class="skeleton" aria-hidden="true"><div style="width:92%"></div><div style="width:78%"></div><div style="width:54%"></div></div>';

  function analysisSteps(c) {
    const p = c.progress || { stage: "plan", done: 0, total: 1 };
    const order = ["plan", "search", "write"], at = order.indexOf(p.stage);
    const st = (i) => (i < at ? "done" : i === at ? "now" : "");
    return `<ol class="steps" aria-label="进度">
      ${step(st(0), "把问题拆成几个要分别查的方面", at === 0 ? "通常 5–15 秒" : "")}
      ${step(st(1), `分方面查找资料${p.stage === "search" ? `（${p.done} / ${p.total}）` : ""}`, at > 1 && c.sources.length ? `找到 ${new Set(c.sources.map((s) => s.file)).size} 份资料，右侧可以先看` : "")}
      ${step(st(2), "按方面写分析、综合结论并标注出处", at === 2 ? "需要读的资料较多，通常 20–40 秒后开始出字" : "")}</ol>${SKELETON}`;
  }

  function extractSteps(c) {
    const p = c.progress || {}, have = c.sources.length > 0, batching = p.stage === "extract";
    const finished = batching && p.done === p.total;
    return `<ol class="steps" aria-label="进度">
      ${step(have ? "done" : "now", have ? `已读取 ${c.sources.length} 个片段（${new Set(c.sources.map((s) => s.file)).size} 份资料）` : (c.scope && c.scope.path ? "正在读取这份文件" : "正在查找相关资料"), have ? "右侧可以先看原文" : "")}
      ${step(finished ? "done" : (have ? "now" : ""), `逐批提取${batching ? `（${p.done} / ${p.total} 批）` : ""}`, have && !finished ? "每批大约 15–40 秒，可以随时停止" : "")}
      ${step(finished ? "now" : "", "整理成表格，每行标出处", "")}</ol>${SKELETON}`;
  }

  function renderQA() {
    const col = $("qa-col"), c = S.cur;
    let h = modelBanner();
    if (!c) {
      const st = S.stats || { files: 0, chunks: 0 };
      if (!S.sources.length) {
        h += `<div class="welcome"><h1>先添加资料文件夹</h1>
          <p>选一个放着 PDF、Word、PowerPoint（.pptx）或 Markdown 的文件夹，建好索引后就可以直接提问。文档内容不会离开这台电脑。</p>
          <button type="button" class="btn primary" data-action="add">添加文件夹</button></div>`;
      } else {
        h += `<div class="welcome"><h1>问你的资料</h1>
          <p>已索引 ${fmt(st.files)} 个文件、${fmt(st.chunks)} 个文本块。直接输入问题，回答会标出处，点编号可以核对原文。</p></div>`;
      }
      col.innerHTML = h;
      return;
    }
    h += questionBlock(c);
    if (c.phase === "search" && c.autoKeyword) {
      h += `<div class="notice"><div><div class="h">这看起来是关键词，已直接搜索原文</div>
        <div class="d">想让模型回答，请输入完整的问题，例如“${esc(c.question)} 在运营商业务里有哪些应用？”</div>
        <div class="chips" style="margin-top:8px"><button type="button" class="btn small" data-action="ask-anyway">仍然让模型回答</button></div></div></div>`;
    }
    if (c.phase === "search") {
      h += `<div class="found"><span>${c.results.length ? `找到 ${c.results.length} 个相关片段 · ${sec(c.ms)}` : "没有找到相关内容"}</span></div>`;
      h += `<div class="card-list">${c.results.map((r) => hitCard(r)).join("")}</div>`;
    } else if (c.kind === "analysis" && (c.phase === "retrieving" || c.phase === "reading")) {
      h += analysisSteps(c);
    } else if (c.kind === "extract" && (c.phase === "retrieving" || c.phase === "reading")) {
      h += extractSteps(c);
    } else if (c.kind === "summary" && (c.phase === "retrieving" || c.phase === "reading")) {
      const p = c.progress || {};
      const parts = c.sources.length;
      const mapping = p.stage === "map", final = p.stage === "final";
      h += `<ol class="steps" aria-label="进度">
        ${step(parts ? "done" : "now", parts ? `已读取全文，分成 ${parts} 个部分` : "正在读取全文", parts ? "右侧可以先看各部分原文" : "")}
        ${p.total > 1 || mapping ? step(final ? "done" : (mapping ? "now" : ""), `逐部分提炼要点${mapping ? `（${p.done} / ${p.total}）` : ""}`,
          mapping ? "每部分大约 15–30 秒，可以随时停止" : "") : ""}
        ${step(final ? "now" : "", "汇总成全文总结并标注出处", final ? "通常 10–20 秒后开始出字" : "")}</ol>
        <div class="skeleton" aria-hidden="true"><div style="width:92%"></div><div style="width:78%"></div><div style="width:54%"></div></div>`;
    } else if (c.phase === "retrieving" || c.phase === "reading") {
      const reading = c.phase === "reading";
      const waited = reading ? Math.floor((Date.now() - c.readingSince) / 1000) : 0;
      h += `<ol class="steps" aria-label="进度">
        <li class="${reading ? "done" : "now"}"><span class="ic" aria-hidden="true">${reading ? checkSvg() : ""}</span>
          <span class="label">${reading ? `已找到 ${new Set(c.sources.map((s) => s.file)).size} 份相关资料` : "正在查找资料"}</span>
          ${reading ? `<span class="sub">${sec(c.retrievalMs)} · 右侧可以先看</span>` : ""}</li>
        <li class="${reading ? "now" : ""}"><span class="ic" aria-hidden="true"></span><span class="label">模型正在阅读资料</span>
          ${reading ? `<span class="sub" id="waited">已等待 ${waited} 秒 · 通常 5–10 秒后开始出字</span>` : ""}</li>
        <li><span class="ic" aria-hidden="true"></span><span class="label">生成回答并标注出处</span></li></ol>
        <div class="skeleton" aria-hidden="true"><div style="width:92%"></div><div style="width:78%"></div><div style="width:54%"></div></div>`;
    } else if (c.phase === "streaming") {
      if (c.sources && c.sources.length) h += foundPills(c.sources);
      h += `<div class="answer" id="answer-live" aria-live="polite">${answerHTML(c.text, true)}</div>`;
    } else if (c.phase === "stopped") {
      h += `<div class="answer">${answerHTML(c.text || "", false)}</div><div class="meta">已停止生成</div>`;
    } else if (c.phase === "error") {
      h += `<div class="banner" role="alert">${esc(c.error)}</div>`;
    } else if (c.phase === "done") {
      const r = c.record;
      h += r.kind === "extract" ? extractHTML(r) : (r.not_found ? notFoundHTML(r) : doneHTML(r));
    }
    col.innerHTML = h;
  }

  function checkSvg() {
    return '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 6.2l2.3 2.3 4.7-5"/></svg>';
  }

  function doneHTML(r) {
    const isSummary = r.kind === "summary" || /^总结：《/.test(r.question);
    let h = !r.context.length ? "" : isSummary
      ? `<div class="found"><span>按 ${r.context.length} 个部分汇总 · 点编号查看对应原文</span></div>` : foundPills(r.context);
    if (r.kind === "analysis" && (r.extra.aspects || []).length) {
      h += `<div class="aspects"><span>分 ${r.extra.aspects.length} 个方面分析：</span>${r.extra.aspects.map((a) => `<span class="pill">${esc(a)}</span>`).join("")}</div>`;
    }
    h += `<div class="answer">${answerHTML(r.answer, false)}</div>`;
    const parts = [];
    if (r.ttft_ms != null) parts.push("首字 " + sec(r.ttft_ms));
    if (r.latency_ms != null) parts.push("共 " + sec(r.latency_ms));
    if (r.model) parts.push(esc(r.model.replace(/^local\//, "")) + "（本机）");
    if (!r.cited.length) parts.push("回答没有标注出处，请在右侧核对原文");
    h += `<div class="meta"><span>${parts.join(" · ")}</span><span class="grow"></span>
      ${isSummary ? "" : '<button type="button" class="btn small" data-action="follow">追问</button>'}
      <button type="button" class="btn small" data-action="copy">复制回答</button>
      <button type="button" class="btn small" data-action="again">${isSummary ? "重新总结" : r.kind === "analysis" ? "重新分析" : "重新回答"}</button></div>`;
    return h;
  }

  function extractHTML(r) {
    const cols = r.extra.columns || [], rows = r.extra.rows || [];
    if (!rows.length) {
      return `<div class="notice"><div><div class="h">没有提取到符合要求的信息</div>
        <div class="d">${r.context.length ? `已读了 ${r.context.length} 个片段，没有找到能填进表格的内容。可以换个说法，或写明字段，例如“字段：项目、金额、负责人”。` : "没有找到相关资料。"}</div></div></div>
        <div class="chips"><button type="button" class="btn" data-action="again">重新提取</button><button type="button" class="btn" data-action="search-instead">改用搜索模式找关键词</button></div>`;
    }
    let h = r.context.length ? foundPills(r.context) : "";
    h += `<div class="xtab-wrap"><table class="xtab"><thead><tr>${cols.map((c) => `<th scope="col">${esc(c)}</th>`).join("")}<th scope="col">出处</th></tr></thead><tbody>`;
    for (const row of rows) {
      h += `<tr>${cols.map((c) => { const v = row.values[c] || ""; return `<td${v ? "" : ' class="empty"'}>${v ? esc(v) : "—"}</td>`; }).join("")}
        <td class="refs">${row.refs.map((n) => `<button type="button" class="ref${n === S.sel ? " on" : ""}" data-ref="${n}" aria-label="出处 ${n}">${n}</button>`).join("") || "—"}</td></tr>`;
    }
    h += "</tbody></table></div>";
    const parts = [`${rows.length} 行 · ${cols.length} 列`];
    if (r.latency_ms != null) parts.push("共 " + sec(r.latency_ms));
    if (r.model) parts.push(esc(r.model.replace(/^local\//, "")) + "（本机）");
    if (rows.some((x) => !x.refs.length)) parts.push("有的行没有标出处，请核对原文");
    h += `<div class="meta"><span>${parts.join(" · ")}</span><span class="grow"></span>
      <button type="button" class="btn small primary" data-action="export-csv">导出 CSV</button>
      <button type="button" class="btn small" data-action="copy-table">复制表格</button>
      <button type="button" class="btn small" data-action="again">重新提取</button></div>`;
    return h;
  }

  function notFoundHTML(r) {
    let h = `<div class="notice"><svg aria-hidden="true" width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="9" cy="9" r="5.5"/><path d="M13.2 13.2L17 17"/></svg>
      <div><div class="h">资料里没有找到这个问题的答案</div>
      <div class="d">为了不编造，模型不会猜测。${r.context.length ? "下面是最接近的片段，可能与问题无关，请自己判断。" : ""}</div></div></div>`;
    if (r.context.length) {
      h += `<section class="card-list" aria-label="最接近的片段"><h2 class="sec-title">最接近的 ${Math.min(3, r.context.length)} 个片段</h2>
        ${r.context.slice(0, 3).map((s) => hitCard(s)).join("")}</section>`;
    }
    h += `<section class="card-list" aria-label="可以试试"><h2 class="sec-title">可以试试</h2><div class="chips">
      <button type="button" class="btn" data-action="search-instead">改用搜索模式找关键词</button>
      <button type="button" class="btn" data-action="add">添加更多文件夹</button>
      ${S.problemsCount ? `<button type="button" class="btn" data-action="lib">有 ${S.problemsCount} 个文件没能索引，查看</button>` : ""}
      </div></section>`;
    return h;
  }

  function hitCard(s) {
    const snip = s.text.replace(/\s+/g, " ").slice(0, 160);
    return `<button type="button" class="hit${s.n === S.sel ? " on" : ""}" data-sel="${s.n}">
      <div class="f">${esc(s.file)} <span class="l">${s.loc ? "· " + esc(s.loc) : ""}</span></div>
      <div class="s">${esc(snip)}</div></button>`;
  }

  // ---------------- 右栏：出处 ----------------
  function currentSources() {
    const c = S.cur;
    if (!c) return [];
    if (c.phase === "search") return c.results;
    if (c.phase === "done") return c.record.context;
    return c.sources || [];
  }

  function highlight(text, answer, n) {
    // 从回答中引用 [n] 的句子里取出数字（含单位）与较长的词，在原文里标出
    let body = esc(text);
    if (!answer) return body;
    const sentences = answer.split(/(?<=[。！？!?；;\n])/).filter((s) => new RegExp(`\\[[^\\]]*\\b${n}\\b[^\\]]*\\]`).test(s));
    const terms = new Set();
    for (const s of sentences) {
      const clean = s.replace(/\[[^\]]*\]/g, "");
      (clean.match(/\d+(?:[.,]\d+)*\s*(?:%|台|张|卡|万|亿|个|项|份|年|月|GB|TB|kW|EFLOPS|bn|Bn|M|K|x)?/g) || []).forEach((t) => { if (t.trim().length >= 2) terms.add(t.trim()); });
    }
    // 没有数字时退而标出较长的英文词（最多 6 个），避免整段都被标黄
    if (![...terms].some((t) => text.includes(t))) {
      for (const s of sentences) {
        (s.replace(/\[[^\]]*\]/g, "").match(/[A-Za-z][A-Za-z0-9.\-]{5,}/g) || []).forEach((t) => terms.add(t));
      }
    }
    const list = [...terms].filter((t) => text.includes(t)).sort((a, b) => b.length - a.length).slice(0, 6);
    if (!list.length) return body;
    const re = new RegExp(list.map((t) => esc(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g");
    return body.replace(re, (m) => `<mark>${m}</mark>`);
  }

  function renderAside() {
    const a = $("aside"), list = currentSources(), c = S.cur;
    if (!c || !list.length) {
      a.innerHTML = `<h2>出处</h2><div class="aside-empty">${c && (c.phase === "retrieving") ? "正在查找资料…" : "提问后，这里显示回答依据的原文。点回答里的编号可以切换。"}</div>`;
      return;
    }
    const answer = c.phase === "done" ? c.record.answer : (c.text || "");
    const title = c.phase === "search" ? "搜索结果" : (c.phase === "done" ? "出处" : "已找到的资料");
    const sub = c.phase === "search" ? "点卡片查看原文" : (c.phase === "done" ? "点回答里的编号切换" : "模型正在阅读这些片段");
    let h = `<div><h2>${title}</h2><div class="sub">${sub}</div></div>`;
    for (const s of list) {
      if (s.n === S.sel) {
        h += `<div class="src on" aria-current="true">
          <div class="top"><span class="num">[${s.n}]</span><span class="name">${esc(s.file)}</span></div>
          ${s.loc ? `<div class="loc">${esc(s.loc)}</div>` : ""}
          <div class="src-tags" id="src-tags" data-path="${esc(s.path)}"></div>
          <div class="body">${highlight(s.text, answer, s.n)}</div>
          <div class="acts">
            <button type="button" class="btn primary" data-open="${esc(s.path)}">${s.type === "pdf" && s.page ? `打开（第 ${s.page} 页）` : "打开原文件"}</button>
            <button type="button" class="btn" data-reveal="${esc(s.path)}">在访达中显示</button>
            <button type="button" class="btn" data-summarize="${esc(s.path)}">总结这份文件</button>
            <button type="button" class="btn" data-scope-file="${esc(s.path)}">只问这份文件</button>
            <button type="button" class="btn" data-extract-file="${esc(s.path)}">从这份文件提取</button>
            <button type="button" class="btn" data-copy-src="${s.n}">复制原文</button>
          </div></div>`;
      } else {
        const snip = s.text.replace(/\s+/g, " ").slice(0, 90);
        h += `<button type="button" class="src" data-sel="${s.n}">
          <div class="top"><span class="num">[${s.n}]</span><span class="name">${esc(s.file)}</span></div>
          ${s.loc ? `<div class="loc">${esc(s.loc)}</div>` : ""}
          <div class="snip">${esc(snip)}</div></button>`;
      }
    }
    a.innerHTML = h;
    loadSrcTags();
  }

  async function loadSrcTags(list) {
    const el = $("src-tags");
    if (!el) return;
    const path = el.dataset.path;
    if (!list) { try { list = await api("file_tags", path); } catch (e) { return; } }
    if (!$("src-tags") || $("src-tags").dataset.path !== path) return;
    $("src-tags").innerHTML = list.map((t) => `<span class="tag${t.origin === "user" ? " user" : ""}" title="${t.origin === "user" ? "手动添加" : "自动生成"}">${esc(t.tag)}<button type="button" data-tag-remove="${esc(t.tag)}" aria-label="删除标签 ${esc(t.tag)}">×</button></span>`).join("") +
      `<form class="tag-add" data-tag-add="1"><label class="sr-only" for="tag-input">添加标签</label><input id="tag-input" maxlength="16" placeholder="＋ 标签"></form>`;
  }

  function select(n) {
    S.sel = Number(n);
    renderAside();
    if (S.cur && (S.cur.phase === "done" || S.cur.phase === "search")) renderQA();
    const on = $("aside").querySelector(".src.on");
    if (on) on.scrollIntoView({ block: "nearest" });
  }

  // ---------------- 提问与搜索 ----------------
  function setGenerating(on) {
    $("composer").hidden = on;
    $("generating").hidden = !on;
    if (!on) $("q").focus();
  }

  // 只有一两个关键词、没有问句特征的输入（如“AI”“5G FWA”），模型很难判断要回答什么，
  // 多半回答“没有找到”。这类输入直接搜索原文，并提示怎样提问。
  const QUESTION_HINT = /[?？]|什么|怎么|怎样|如何|为什么|为何|哪|谁|多少|几|是否|吗|呢|是不是|有没有|介绍|总结|概括|说明|解释|比较|区别|列出|列举|告诉|分析|\b(what|how|why|who|which|when|where|is|are|does|do|can|list|explain|summari[sz]e|compare)\b/i;
  function isKeywordQuery(q) {
    if (QUESTION_HINT.test(q)) return false;
    const cjk = (q.match(/[\u3400-\u9fff]/g) || []).length;
    const words = (q.match(/[A-Za-z0-9][A-Za-z0-9.+\-]*/g) || []).length;
    return cjk <= 6 && words <= 2 && q.length <= 16;
  }

  let ticker = null;
  const GEN_TEXT = {
    qa: "回答生成中，可以先在右侧核对原文", summary: "正在总结整份文件，可以随时停止",
    analysis: "深度分析中（大约 1–2 分钟），可以随时停止", extract: "正在提取，可以随时停止",
  };

  // 开始一个后台任务（问答 / 总结 / 分析 / 提取）：先显示进度，等 Python 推送事件
  async function startJob(kind, question, call, extra) {
    if (!$("generating").hidden) { toast("上一个任务还在进行，请稍等或先停止"); return; }
    if (S.view !== "qa") showView("qa");
    S.cur = Object.assign({ phase: "retrieving", kind, question, text: "", sources: [] }, extra || {});
    S.sel = null;
    $("gen-text").textContent = GEN_TEXT[kind] || GEN_TEXT.qa;
    renderQA(); renderAside(); renderSide();
    setGenerating(true);
    try {
      const r = await call();
      if (r.error) { S.cur.phase = "error"; S.cur.error = r.error; setGenerating(false); renderQA(); return; }
      S.cur.qid = r.qid;
    } catch (e) {
      S.cur.phase = "error"; S.cur.error = String(e.message || e); setGenerating(false); renderQA();
    }
    if (kind !== "qa") return;
    clearInterval(ticker);
    ticker = setInterval(() => {
      const w = $("waited");
      if (!S.cur || S.cur.phase !== "reading") { if (!S.cur || S.cur.phase !== "retrieving") clearInterval(ticker); return; }
      if (w) w.textContent = `已等待 ${Math.floor((Date.now() - S.cur.readingSince) / 1000)} 秒 · 通常 5–10 秒后开始出字`;
    }, 1000);
  }

  function askJob(q, scope, followUp) {
    return startJob("qa", q, () => api("ask", q, scope, followUp || null), { scope, followUp });
  }
  function analyzeJob(q, scope) {
    return startJob("analysis", "分析：" + q, () => api("analyze", q, scope), { scope });
  }
  function extractJob(q, path, scope) {
    const label = "提取：" + q + (path ? `（《${baseName(path)}》）` : "");
    return startJob("extract", label, () => api("extract", q, path || null, path ? null : scope), { scope: path ? { path } : scope });
  }

  async function submit(text, forceAsk) {
    const q = (text != null ? text : $("q").value).trim();
    if (!q) return;
    if (!$("generating").hidden) { toast("上一个任务还在进行，请稍等或先停止"); return; }
    if (S.view !== "qa") showView("qa");
    $("q").value = "";
    S.scopeOpen = false;
    const scope = scopeDict(), follow = S.followUp;
    const autoKeyword = S.mode === "ask" && !forceAsk && !follow && isKeywordQuery(q);
    if (S.mode === "search" || autoKeyword) {
      S.cur = { phase: "search", question: q, results: [], ms: 0, autoKeyword, scope };
      try {
        const r = await api("search", q, scope);
        S.cur.results = r.results; S.cur.ms = r.ms;
        S.sel = r.results.length ? 1 : null;
      } catch (e) { S.cur = { phase: "error", question: q, error: String(e.message || e) }; }
      renderScope(); renderQA(); renderAside(); renderSide();
      return;
    }
    S.followUp = null; renderFollow(); renderScope();
    if (S.mode === "extract") return extractJob(q, S.scope.path, scope);
    if (S.mode === "analyze") return analyzeJob(q, scope);
    return askJob(q, scope, follow && follow.id);
  }

  function summarizeFile(path) {
    return startJob("summary", `总结：《${baseName(path)}》`, () => api("summarize", path));
  }

  function renderFollow() {
    const f = $("follow"), u = S.followUp;
    f.hidden = !u;
    f.innerHTML = u ? `<span>追问：${esc(u.question)}</span><button type="button" data-action="follow-cancel" aria-label="取消追问">×</button>` : "";
  }

  // 重新执行一条记录（按它的类型和当时的范围）
  function rerun(r) {
    const ex = r.extra || {}, sc = ex.scope && Object.keys(ex.scope).length ? ex.scope : null;
    if (r.kind === "summary" || /^总结：《/.test(r.question)) return summarizeFile(ex.path || (r.context[0] && r.context[0].path));
    if (r.kind === "extract") return extractJob(ex.request || r.question.replace(/^提取：/, ""), ex.path, sc);
    if (r.kind === "analysis") return analyzeJob(ex.query || r.question.replace(/^分析：/, ""), sc);
    return askJob(r.question, sc, ex.follow_up);
  }

  const PLACEHOLDER = {
    ask: "问一个关于你资料的问题，⌘↩ 发送",
    search: "输入关键词，只搜索原文（不生成回答，很快）",
    extract: "要整理成表格的信息，例如“列出各项目的名称、预算和负责人”，或写明“字段：客户、金额、日期”",
    analyze: "要深入分析的问题，例如“二期项目有哪些主要风险、怎么应对？”（大约 1–2 分钟）",
  };
  function setMode(mode) {
    S.mode = mode;
    document.querySelectorAll(".composer .seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
    $("q").placeholder = PLACEHOLDER[mode] || PLACEHOLDER.ask;
    if (mode !== "ask" && S.followUp) { S.followUp = null; renderFollow(); }
    $("q").focus();
  }

  function newQuestion() {
    if (!$("generating").hidden) return;
    S.cur = null; S.sel = null;
    showView("qa");
    renderQA(); renderAside(); renderSide();
    $("q").focus();
  }

  async function openHistory(id) {
    if (!$("generating").hidden) { toast("正在回答，请稍等或先停止"); return; }
    const r = await api("open_history", Number(id));
    if (!r) { toast("这条记录已不存在"); loadHistory(); return; }
    S.cur = { phase: "done", record: r, question: r.question, kind: r.kind };
    S.sel = r.cited.length ? r.cited[0] : (r.context.length ? 1 : null);
    showView("qa");
    renderQA(); renderAside(); renderSide();
  }

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast("已复制"); }
    catch (e) {
      const t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select();
      document.execCommand("copy"); t.remove(); toast("已复制");
    }
  }

  // ---------------- 资料库 ----------------
  function showView(v) {
    if (v === "logs" && !S.settings.dev_mode) v = "settings";
    S.view = v;
    $("view-qa").hidden = v !== "qa"; $("aside").hidden = v !== "qa";
    $("view-lib").hidden = v !== "lib"; $("lib-aside").hidden = v !== "lib";
    $("view-settings").hidden = v !== "settings"; $("view-logs").hidden = v !== "logs";
    $("btn-settings").classList.toggle("active", v === "settings");
    $("btn-logs").classList.toggle("active", v === "logs");
    if (v === "lib") { renderLib(); refreshLibrary(); }
    if (v === "settings") renderSettings();
    if (v === "logs") { renderLogs(); pollLogs(); }
    renderSide();
  }

  // ---------------- 设置 ----------------
  function applyDevMode() {
    $("btn-logs").hidden = !S.settings.dev_mode;
    if (S.settings.dev_mode) startLogPolling(); else stopLogPolling();
  }

  function renderSettings() {
    const on = !!S.settings.dev_mode;
    const models = S.models.map((m) => `<span class="k">${esc(m.ref)}</span><span class="v">${m.ok ? "就绪" : "不可用"}${m.message ? "：" + esc(m.message) : ""}</span>`).join("");
    $("settings").innerHTML = `<div class="lib-head"><h1>设置</h1></div>
      <section class="setting">
        <div class="txt"><div class="h" id="dev-label">开发者模式</div>
          <div class="d">打开后左栏出现“日志”，可以实时查看运行日志（检索到哪些片段、各自分数、首字时间、token 数、错误）和每次模型调用的耗时与内存。
          同时记录更详细的调试信息。日志只保存在本机的 data/logs/，不记录文档全文。</div></div>
        <button type="button" class="switch" role="switch" aria-checked="${on}" aria-labelledby="dev-label" id="dev-switch"></button>
      </section>
      <section class="setting">
        <div class="txt"><div class="h" id="tags-label">自动生成标签</div>
          <div class="d">索引完成后，在电脑空闲时让模型读每份文件的开头，给出 3–5 个主题标签。提问时可以按标签限定范围；出处卡片里也可以手动加、删标签。
          每份文件大约 3–8 秒，资料多时需要较长时间，问答时自动让路。</div></div>
        <button type="button" class="switch" role="switch" aria-checked="${!!S.settings.auto_tags}" aria-labelledby="tags-label" id="tags-switch"></button>
      </section>
      <section class="setting" id="daemon-setting" hidden></section>
      <section class="setting"><div class="txt"><div class="h">运行信息</div>
        <div class="kv" style="margin-top:8px">${models}
          <span class="k">日志文件</span><span class="v">${esc(S.log.file || "data/logs/lkos.log")}</span></div>
        <div class="chips" style="margin-top:12px"><button type="button" class="btn small" id="settings-open-logs">打开日志文件夹</button></div>
      </div></section>`;
    $("dev-switch").addEventListener("click", async () => {
      S.settings = await api("set_setting", "dev_mode", !on);
      applyDevMode(); renderSettings();
      toast(S.settings.dev_mode ? "开发者模式已打开，左栏可以看日志" : "开发者模式已关闭");
    });
    $("settings-open-logs").addEventListener("click", openLogsFolder);
    $("tags-switch").addEventListener("click", async () => {
      S.settings = await api("set_setting", "auto_tags", !S.settings.auto_tags);
      renderSettings();
      toast(S.settings.auto_tags ? "已打开：空闲时自动生成标签" : "已关闭自动生成标签");
    });
    renderDaemon();
  }

  const DAEMON_STATE = { idle: "等待中，到时间会自动更新", running: "正在更新", "app-open": "应用开着，由应用负责索引", stopped: "没有运行" };
  async function renderDaemon(busy) {
    const el = $("daemon-setting");
    if (!el) return;
    let st;
    try { st = await api("daemon_status"); } catch (e) { return; }
    el.hidden = false;
    let state = DAEMON_STATE[st.state] || st.state || "";
    if ((st.state || "").startsWith("paused:")) state = "已暂停：" + (REASON[st.state.slice(7)] || st.state.slice(7));
    const last = st.last ? (st.last.error ? `最近一次出错：${esc(st.last.error)}` : `最近一次：${esc(baseName(st.last.folder))} 新索引 ${fmt(st.last.indexed)} 个文件`) : "";
    el.innerHTML = `<div class="txt"><div class="h" id="daemon-label">关掉窗口后继续索引</div>
        <div class="d">${st.supported ? `安装一个后台服务（登录后自动运行），关掉窗口后仍按各文件夹的周期，在接通电源、电脑空闲时更新索引；打开应用时自动让给应用。
          ${st.installed ? `<br>状态：${esc(state)}${st.folder ? "（" + esc(baseName(st.folder)) + "）" : ""}${last ? " · " + last : ""}` : ""}`
          : "只支持 macOS（用系统的 launchd 在后台运行）。"}</div></div>
      <button type="button" class="switch" role="switch" aria-checked="${!!st.installed}" aria-labelledby="daemon-label" id="daemon-switch"${st.supported && !busy ? "" : " disabled"}></button>`;
    $("daemon-switch").addEventListener("click", async () => {
      renderDaemon(true);
      const r = await api("set_daemon", !st.installed);
      if (r.error) toast(r.error); else toast(r.installed ? "后台服务已安装，关掉窗口后会继续索引" : "后台服务已卸载");
      renderDaemon();
    });
  }

  async function openLogsFolder() {
    const r = await api("open_logs_folder");
    if (r.error) toast(r.error);
  }

  // ---------------- 日志 ----------------
  let logTimer = null;
  function startLogPolling() {
    if (logTimer) return;
    logTimer = setInterval(pollLogs, 1000);
    pollLogs();
  }
  function stopLogPolling() { clearInterval(logTimer); logTimer = null; }

  let logBusy = false;
  async function pollLogs() {
    const L = S.log;
    if (logBusy) return;
    logBusy = true;
    try {
      if (L.tab === "calls" && S.view === "logs") {
        L.calls = await api("calls", 100);
        renderLogs();
        return;
      }
      const r = await api("logs", L.after, L.level);
      L.file = r.file;
      if (r.lines.length) {
        L.lines.push(...r.lines);
        if (L.lines.length > 3000) L.lines.splice(0, L.lines.length - 3000);
        L.after = r.lines[r.lines.length - 1].seq;
        if (S.view !== "logs") {
          L.warn += r.lines.filter((x) => x.level === "WARNING" || x.level === "ERROR" || x.level === "CRITICAL").length;
          $("logs-badge").textContent = L.warn ? String(L.warn) : "";
        } else {
          appendLogLines(r.lines);
        }
      }
    } catch (e) { /* 窗口关闭或还没准备好 */ }
    finally { logBusy = false; }
  }

  function logLineHTML(x) {
    return `<div class="log-line lv-row-${x.level}"><span class="tm">${x.time}.${String(x.ms).padStart(3, "0")}</span>` +
      `<span class="lv lv-${x.level}">${x.level === "WARNING" ? "WARN" : x.level}</span>` +
      `<span class="lsrc" title="${esc(x.source)}">${esc(x.source)}</span><span class="msg">${esc(x.msg)}</span></div>`;
  }

  function matchFilter(x) {
    const f = S.log.filter.trim().toLowerCase();
    return !f || x.msg.toLowerCase().includes(f) || x.source.toLowerCase().includes(f);
  }

  function appendLogLines(lines) {
    const body = $("log-body");
    if (!body || S.log.tab !== "run") return;
    const empty = body.querySelector(".log-empty");
    if (empty) empty.remove();
    body.insertAdjacentHTML("beforeend", lines.filter(matchFilter).map(logLineHTML).join(""));
    while (body.children.length > 3000) body.firstChild.remove();
    if ($("log-auto").checked) body.scrollTop = body.scrollHeight;
    $("log-foot").textContent = `共 ${S.log.lines.length} 条 · 日志文件：${S.log.file}`;
  }

  function renderLogs() {
    const L = S.log, body = $("log-body");
    L.warn = 0; $("logs-badge").textContent = "";
    document.querySelectorAll("[data-logtab]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.logtab === L.tab)));
    ["log-level", "log-filter", "log-clear"].forEach((id) => { $(id).disabled = L.tab !== "run"; });
    if (L.tab === "calls") {
      const rows = L.calls.map((c) => `<tr><td>${esc((c.ts || "").replace("T", " ").slice(5, 19))}</td><td>${esc(c.task)}</td><td>${esc(c.model)}</td>
        <td class="num">${c.ttft_ms != null ? (c.ttft_ms / 1000).toFixed(1) + " s" : "-"}</td>
        <td class="num">${c.latency_ms != null ? (c.latency_ms / 1000).toFixed(1) + " s" : "-"}</td>
        <td class="num">${c.input_tokens != null ? fmt(c.input_tokens) + " / " + fmt(c.output_tokens) : "-"}</td>
        <td class="num">${c.mem_available_mb != null ? fmt(c.mem_available_mb) + " MB" : "-"}</td>
        <td class="num">${c.swap_used_mb != null ? (c.swap_used_mb / 1024).toFixed(1) + " GB" : "-"}</td>
        <td class="err">${esc(c.error || "")}</td></tr>`).join("");
      body.innerHTML = L.calls.length ? `<table class="calls"><thead><tr><th>时间（UTC）</th><th>任务</th><th>模型</th><th>首字</th><th>总耗时</th><th>输入 / 输出 tokens</th><th>可用内存</th><th>交换区</th><th>错误</th></tr></thead><tbody>${rows}</tbody></table>`
        : '<div class="log-empty">还没有模型调用记录。</div>';
      $("log-foot").textContent = "最近 100 次模型调用（问答、计算向量），每秒刷新";
      return;
    }
    const lines = L.lines.filter(matchFilter);
    body.innerHTML = lines.length ? lines.map(logLineHTML).join("") : '<div class="log-empty">暂时没有日志。提一个问题或更新索引，这里会实时出现记录。</div>';
    if ($("log-auto").checked) body.scrollTop = body.scrollHeight;
    $("log-foot").textContent = `共 ${L.lines.length} 条 · 日志文件：${L.file}`;
  }

  function renderLib() {
    const lib = S.lib, el = $("lib");
    if (!lib) { el.innerHTML = "<p>正在读取资料库…</p>"; return; }
    const i = S.index || {};
    const total = lib.sources.reduce((a, s) => a + s.files, 0);
    let h = `<div class="lib-head"><h1>资料库</h1><span class="sub">${lib.sources.length} 个文件夹 · ${fmt(total)} 个文件 · ${fmt(lib.stats.chunks)} 个文本块</span>
      <span class="grow"></span>
      ${lib.sources.length ? '<button type="button" class="btn" data-action="update-all">全部立即更新</button>' : ""}
      <button type="button" class="btn primary" data-action="add">添加文件夹</button></div>
      <div class="drop" id="drop">把文件夹拖到这里添加 · 支持 PDF、Word、PowerPoint（.pptx）、Markdown</div>`;
    if (!lib.sources.length) h += '<p class="sub">还没有添加文件夹。</p>';
    for (const s of lib.sources) {
      const name = s.path.split("/").filter(Boolean).pop() || s.path;
      const active = i.folder === s.path && (i.state === "running" || i.state === "paused");
      const last = s.last_indexed ? new Date(s.last_indexed).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : null;
      let state;
      if (active) state = `<span class="state" style="color:var(--run-ink)">${i.kind === "tags" ? "生成标签中" : s.last_indexed ? "更新中" : "首次索引中"}</span>`;
      else if (!s.last_indexed) state = `<span class="state" style="color:var(--warn-ink)"><span class="dot" style="background:var(--warn-dot)"></span>还没索引完</span>`;
      else state = `<span class="state" style="color:var(--ok-ink)"><span class="dot" style="background:var(--ok-dot)"></span>已是最新</span>`;
      h += `<section class="folder" aria-label="${esc(name)}"><div class="row"><h2>${esc(name)}</h2><span class="path">${esc(s.path)}</span><span class="grow"></span>${state}</div>`;
      if (active) {
        const pct = i.total ? Math.floor((i.done / i.total) * 100) : 0;
        h += `<div class="bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100" aria-label="索引进度"><div style="width:${pct}%"></div></div>
          <div class="facts"><span>${fmt(i.done)} / ${fmt(i.total)} 个文件</span><span>已可检索 ${fmt(s.chunks)} 个文本块</span><span>最近修改的文件优先</span>${i.file ? `<span class="path">${esc(i.file)}</span>` : ""}</div>`;
        if (i.state === "paused") {
          h += `<div class="pausebox"><span class="dot" aria-hidden="true"></span><span class="t">已暂停：${esc(REASON[i.reason] || "")}</span>
            ${i.reason === "manual" ? '<button type="button" class="btn small" data-action="resume">继续</button>' : ""}</div>`;
        }
      } else {
        h += `<div class="facts"><span>${fmt(s.files)} 个文件</span><span>${fmt(s.chunks)} 个文本块</span>
          ${s.failed ? `<span style="color:var(--danger)">失败 ${s.failed}</span>` : ""}
          <span>${last ? "上次更新 " + esc(last) : "下次打开或空闲时继续"}</span><span>${REFRESH_LABEL[s.refresh] || "每周自动更新"}</span>
          ${s.exclude.patterns.length ? `<span title="${esc(s.exclude.patterns.join("、"))}">排除 ${s.exclude.patterns.length} 条规则</span>` : ""}
          ${s.exclude.types.length ? `<span>不索引 ${SKIP_TYPES.filter((t) => t[2].some((x) => s.exclude.types.includes(x))).map((t) => t[1]).join("、")}</span>` : ""}
          ${s.ocr ? "<span>扫描件文字识别：开</span>" : ""}
          ${s.files ? `<span>标签 ${fmt(s.tagged)} / ${fmt(s.files - s.failed)} 个文件</span>` : ""}</div>`;
      }
      if (S.libEdit === s.id) h += folderSettingsHTML(s, lib.ocr_available);
      const confirming = S.confirmRemove === s.id;
      h += `<div class="row" style="align-items:center">
        <span class="grow"></span>
        ${active ? (i.state === "paused" && i.reason === "manual" ? '<button type="button" class="btn small" data-action="resume">继续</button>' : '<button type="button" class="btn small" data-action="pause">暂停</button>') + '<button type="button" class="btn small" data-action="stop-index">停止</button>'
          : `<button type="button" class="btn small" data-update="${esc(s.path)}">立即更新</button>`}
        ${!active && s.files && s.tagged < s.files - s.failed ? `<button type="button" class="btn small" data-gen-tags="${esc(s.path)}">生成标签</button>` : ""}
        <button type="button" class="btn small" data-folder-settings="${s.id}" aria-expanded="${S.libEdit === s.id}">设置</button>
        ${confirming ? `<span class="sub">只删除索引，不动原文件</span><button type="button" class="btn small danger" data-remove-yes="${s.id}">确认移除</button><button type="button" class="btn small" data-action="remove-no">取消</button>`
          : `<button type="button" class="btn small danger" data-remove="${s.id}">移除</button>`}
        </div></section>`;
    }
    if (lib.tags && lib.tags.length) {
      h += `<section aria-label="标签" style="display:flex;flex-direction:column;gap:8px"><h2 class="sec-title">标签（点一个，只在这些文件里提问）</h2>
        <div class="chips">${lib.tags.slice(0, 60).map((t) => `<button type="button" class="tag-chip" data-tag-filter="${esc(t.tag)}">${esc(t.tag)}<span class="n">${fmt(t.files)}</span></button>`).join("")}</div></section>`;
    }
    if (lib.problems.length) {
      h += `<section aria-label="需要处理的文件" style="display:flex;flex-direction:column;gap:8px"><h2 class="sec-title">需要处理的文件（${lib.problems.length}）</h2><div class="problems">`;
      for (const p of lib.problems.slice(0, 100)) {
        h += `<span class="f">${esc(p.file)}</span><span class="h">${esc(p.hint)}</span><button type="button" class="btn small" data-reveal="${esc(p.path)}">在访达中显示</button>`;
      }
      h += "</div></section>";
    }
    el.innerHTML = h;
    bindDrop();
  }

  const REFRESH_LABEL = { daily: "每天自动更新", weekly: "每周自动更新", monthly: "每月自动更新", manual: "只手动更新" };
  const SKIP_TYPES = [["pdf", "PDF", ["pdf"]], ["word", "Word", ["docx", "doc"]], ["ppt", "PowerPoint", ["pptx", "ppt"]], ["md", "Markdown", ["md"]]];

  function folderSettingsHTML(s, ocrOK) {
    const ex = s.exclude;
    return `<form class="fset" data-fset="${s.id}">
      <label class="fset-row"><span class="k">自动更新</span>
        <select name="refresh" class="mini">${Object.entries(REFRESH_LABEL).map(([v, t]) => `<option value="${v}"${s.refresh === v ? " selected" : ""}>${t.replace("自动更新", "").replace("更新", "") || t}</option>`).join("")}</select>
        <span class="hint">在接通电源、电脑空闲时进行</span></label>
      <div class="fset-row top"><span class="k">排除</span>
        <div class="grow"><textarea name="patterns" rows="3" class="mini" placeholder="每行一条，例如：&#10;归档&#10;*草稿*&#10;2019/*">${esc(ex.patterns.join("\n"))}</textarea>
        <div class="hint">不含“/”的规则匹配任意一级的文件夹名或文件名；含“/”的规则从这个文件夹开始匹配路径。“*”代表任意字符。</div></div></div>
      <div class="fset-row"><span class="k">不索引</span><div class="chips">${SKIP_TYPES.map((t) => `<label class="check"><input type="checkbox" name="type" value="${t[0]}"${t[2].every((x) => ex.types.includes(x)) ? " checked" : ""}> ${t[1]}</label>`).join("")}</div></div>
      <div class="fset-row top"><span class="k">文字识别</span>
        <div class="grow"><label class="check"><input type="checkbox" name="ocr"${s.ocr ? " checked" : ""}${ocrOK || s.ocr ? "" : " disabled"}> 识别扫描版 PDF 里的文字</label>
        <div class="hint">${ocrOK ? "用 macOS 自带的文字识别，完全离线；每页大约 1–2 秒，会让索引变慢。" : "需要 macOS，并运行 source/scripts/setup_mac.sh 安装文字识别组件。"}</div></div></div>
      <div class="fset-row actions"><span class="hint">改了排除规则或文字识别后，会自动重新索引这个文件夹</span>
        <button type="button" class="btn small" data-action="fset-cancel">取消</button><button type="submit" class="btn small primary">保存</button></div>
    </form>`;
  }

  async function saveFolderSettings(form) {
    const id = Number(form.dataset.fset);
    const types = [...form.querySelectorAll('input[name="type"]:checked')].flatMap((x) => SKIP_TYPES.find((t) => t[0] === x.value)[2]);
    const settings = {
      refresh: form.refresh.value,
      exclude: { patterns: form.patterns.value.split("\n").map((x) => x.trim()).filter(Boolean), types },
      ocr: form.ocr.checked,
    };
    const r = await api("set_source_settings", id, settings);
    if (r.error) { toast(r.error); return; }
    S.libEdit = null;
    toast(r.reindex ? "已保存，正在按新设置重新索引" : "已保存");
    refreshLibrary();
  }

  function bindDrop() {
    const d = $("drop");
    if (!d) return;
    d.addEventListener("dragover", (e) => { e.preventDefault(); d.classList.add("over"); });
    d.addEventListener("dragleave", () => d.classList.remove("over"));
    d.addEventListener("drop", (e) => {
      e.preventDefault(); d.classList.remove("over");
      const files = [...(e.dataTransfer && e.dataTransfer.files || [])];
      const paths = files.map((f) => f.pywebviewFullPath).filter(Boolean);
      if (!paths.length) { toast("拖入没有成功，请点“添加文件夹”选择"); return; }
      paths.forEach((p) => addPath(p));
    });
  }

  async function addPath(p) {
    const r = await api("add_folder", p);
    if (r.error) toast(r.error); else toast(`已添加，开始索引 ${fmt(r.files)} 个文件`);
    refreshLibrary();
  }

  async function addFolder() {
    try {
      const r = await api("choose_folder");
      if (r.error) toast(r.error);
      else if (!r.cancelled) { toast(`已添加，开始索引 ${fmt(r.files)} 个文件`); showView("lib"); }
      refreshLibrary();
    } catch (e) { toast(String(e.message || e)); }
  }

  // ---------------- 事件绑定 ----------------
  document.addEventListener("click", async (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    const d = t.dataset;
    if (d.ref) return select(d.ref);
    if (d.sel) return select(d.sel);
    if (d.hid) return openHistory(d.hid);
    if (d.lib) return showView("lib");
    if (d.open != null) { const r = await api("open_file", d.open); if (r.error) toast(r.error); return; }
    if (d.reveal != null) { const r = await api("reveal", d.reveal); if (r.error) toast(r.error); return; }
    if (d.summarize != null) return summarizeFile(d.summarize);
    if (d.scopeFile != null) { setScopeFile(d.scopeFile); if (S.mode !== "ask" && S.mode !== "analyze") setMode("ask"); toast(`接下来只在《${baseName(d.scopeFile)}》里找答案`); $("q").focus(); return; }
    if (d.extractFile != null) { setScopeFile(d.extractFile); setMode("extract"); toast(`写下要从《${baseName(d.extractFile)}》提取的内容`); return; }
    if (d.tagRemove != null) {
      const path = $("src-tags").dataset.path, r = await api("remove_tag", path, d.tagRemove);
      if (r.error) toast(r.error); else loadSrcTags(r.tags);
      return;
    }
    if (d.tagFilter != null) {
      S.scope = { source_ids: [], file_types: [], days: 0, path: null, tags: [d.tagFilter] };
      showView("qa"); renderScope(); toast(`接下来只在标签“${d.tagFilter}”的文件里找`); $("q").focus(); return;
    }
    if (d.genTags != null) { const r = await api("generate_tags", d.genTags); toast(r.ok ? "开始生成标签" : "已经在生成了"); return; }
    if (d.scopeReset) { S.scope = { source_ids: [], file_types: [], days: 0, path: null, tags: [] }; return renderScope(); }
    if (d.scopeClose) { S.scopeOpen = false; renderScope(); $("q").focus(); return; }
    if (d.scopeClearFile) { S.scope.path = null; return renderScope(); }
    if (d.copySrc) { const s = currentSources().find((x) => x.n === Number(d.copySrc)); if (s) copy(s.text); return; }
    if (d.update) { await api("update_source", d.update); toast("已加入更新队列"); return; }
    if (d.remove) { S.confirmRemove = Number(d.remove); return renderLib(); }
    if (d.folderSettings) { S.libEdit = S.libEdit === Number(d.folderSettings) ? null : Number(d.folderSettings); return renderLib(); }
    if (d.removeYes) {
      S.confirmRemove = null; toast("正在移除…");
      const r = await api("remove_source", Number(d.removeYes));
      S.scope.source_ids = S.scope.source_ids.filter((x) => x !== Number(d.removeYes)); renderScope();
      toast(r.error || `已移除 ${fmt(r.removed_files)} 个文件的索引`); return refreshLibrary();
    }
    switch (d.action) {
      case "add": return addFolder();
      case "lib": return showView("lib");
      case "copy": return copy(S.cur.record.answer.replace(/\s*\[\d+(?:\s*[,，、]\s*\d+)*\]/g, ""));
      case "copy-table": return copy(S.cur.record.answer);
      case "export-csv": {
        const r = await api("export_csv", S.cur.record.id);
        if (r.error) toast(r.error); else if (!r.cancelled) toast("已导出：" + baseName(r.path));
        return;
      }
      case "follow":
        S.followUp = { id: S.cur.record.id, question: S.cur.record.question.replace(/^(分析|提取)：/, "") };
        setMode("ask"); renderFollow(); return;
      case "follow-cancel": S.followUp = null; renderFollow(); $("q").focus(); return;
      case "again":
        if (S.cur.record) return rerun(S.cur.record);
        return submit(S.cur.question, true);
      case "ask-anyway": return submit(S.cur.question, true);
      case "search-instead": setMode("search"); $("q").value = S.cur ? S.cur.question : ""; return;
      case "update-all": { const r = await api("update_all"); toast(r.queued ? `${r.queued} 个文件夹已加入更新队列` : "已经在更新了"); return; }
      case "pause": await api("pause_index"); return refreshLibrary();
      case "resume": await api("resume_index"); return refreshLibrary();
      case "stop-index": await api("stop_index"); toast("已停止，下次空闲时或点“立即更新”继续"); return;
      case "remove-no": S.confirmRemove = null; return renderLib();
      case "fset-cancel": S.libEdit = null; return renderLib();
    }
  });

  $("btn-settings").addEventListener("click", () => showView("settings"));
  $("btn-logs").addEventListener("click", () => showView("logs"));
  document.querySelectorAll("[data-logtab]").forEach((b) => b.addEventListener("click", () => {
    S.log.tab = b.dataset.logtab; renderLogs(); pollLogs();
  }));
  $("log-level").addEventListener("change", (e) => { S.log.level = e.target.value; S.log.after = 0; S.log.lines = []; renderLogs(); pollLogs(); });
  $("log-filter").addEventListener("input", (e) => { S.log.filter = e.target.value; renderLogs(); });
  $("log-clear").addEventListener("click", () => { S.log.lines = []; renderLogs(); });
  $("log-copy").addEventListener("click", () => {
    if (S.log.tab === "calls") return copy(JSON.stringify(S.log.calls, null, 2));
    copy(S.log.lines.filter(matchFilter).map((x) => `${x.time}.${String(x.ms).padStart(3, "0")} ${x.level} ${x.source}: ${x.msg}`).join("\n"));
  });
  $("log-folder").addEventListener("click", openLogsFolder);
  $("status-card").addEventListener("click", () => (S.sources.length ? showView("lib") : addFolder()));
  $("btn-new").addEventListener("click", newQuestion);
  $("btn-add-side").addEventListener("click", addFolder);
  $("btn-stop").addEventListener("click", () => api("stop"));
  document.querySelectorAll(".composer .seg button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
  $("scope-btn").addEventListener("click", async () => {
    S.scopeOpen = !S.scopeOpen; renderScope();
    if (S.scopeOpen) { try { S.tags = await api("tags", 40); } catch (e) { /* 忽略 */ } renderScope(); }
  });
  $("scope-pop").addEventListener("change", (e) => {
    const t = e.target, d = t.dataset, sc = S.scope;
    const toggle = (arr, v) => { const i = arr.indexOf(v); if (t.checked && i < 0) arr.push(v); if (!t.checked && i >= 0) arr.splice(i, 1); };
    if (d.scopeSrc) toggle(sc.source_ids, Number(d.scopeSrc));
    else if (d.scopeType) toggle(sc.file_types, d.scopeType);
    else if (d.scopeDays != null) sc.days = Number(d.scopeDays);
    else if (d.scopeTag != null) toggle(sc.tags, d.scopeTag);
    renderScope();
  });
  document.addEventListener("mousedown", (e) => {
    if (S.scopeOpen && !e.target.closest(".scope-wrap")) { S.scopeOpen = false; renderScope(); }
  });
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  $("aside").addEventListener("submit", async (e) => {
    if (!e.target.matches("form.tag-add")) return;
    e.preventDefault();
    const input = e.target.querySelector("input"), path = $("src-tags").dataset.path;
    if (!input.value.trim()) return;
    const r = await api("add_tag", path, input.value);
    if (r.error) toast(r.error); else { loadSrcTags(r.tags); setTimeout(() => { const i = $("tag-input"); if (i) i.focus(); }, 0); }
  });
  $("lib").addEventListener("submit", (e) => {
    if (e.target.matches("form.fset")) { e.preventDefault(); saveFolderSettings(e.target); }
  });
  // 输入：回车换行，⌘+回车（Windows/Linux 上 Ctrl+回车）发送。
  // 输入法保护：中文输入法里用回车选英文、Esc 取消候选时，键盘事件会带 isComposing 或 keyCode 229；
  // Safari / WebKit 还会在 compositionend 之后紧跟一次回车事件，所以输入法结束后 80 ms 内的按键也忽略。
  let compositionEndAt = 0;
  const imeActive = (e) => e.isComposing || e.keyCode === 229 || Date.now() - compositionEndAt < 80;
  $("q").addEventListener("compositionend", () => { compositionEndAt = Date.now(); });
  $("q").addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || imeActive(e)) return;
    if (e.metaKey || e.ctrlKey) { e.preventDefault(); submit(); }
    // 普通回车：保持默认行为（换行）
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && S.scopeOpen && !imeActive(e)) { S.scopeOpen = false; renderScope(); $("scope-btn").focus(); return; }
    if (e.key === "Escape" && !$("generating").hidden) {
      if (imeActive(e)) return;   // 输入法里按 Esc 取消候选词，不是要停止生成
      api("stop"); return;
    }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "n") { e.preventDefault(); newQuestion(); return; }
    if ((e.metaKey || e.ctrlKey) && /^[1-9]$/.test(e.key) && currentSources().some((s) => s.n === Number(e.key))) {
      e.preventDefault(); select(e.key);
    }
  });
  // 整个窗口都可以拖入文件夹（不拖到指定区域也行）
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("drop", (e) => {
    if (e.target.closest && e.target.closest("#drop")) return;
    e.preventDefault();
    const paths = [...(e.dataTransfer && e.dataTransfer.files || [])].map((f) => f.pywebviewFullPath).filter(Boolean);
    paths.forEach((p) => addPath(p));
  });

  if (window.pywebview && window.pywebview.api) boot();
})();
