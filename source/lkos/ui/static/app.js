/* Local Knowledge OS 应用界面。通过 window.pywebview.api 调用 Python（lkos/ui/bridge.py），
   Python 通过 window.lkos.onEvent(事件) 推送问答进度与索引进度。 */
(function () {
  "use strict";

  const S = {
    view: "qa", mode: "ask", models: [], model: "", history: [], sources: [], index: { state: "idle" },
    paused: false, cur: null, sel: null, lib: null, confirmRemove: null, problemsCount: 0,
    settings: { dev_mode: false },
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
        S.cur = { phase: "done", record: e.record, question: e.record.question };
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
    renderSide(); renderStatus(); renderQA(); renderAside();
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
    battery: "正在用电池，接通电源后继续",
  };

  function renderStatus() {
    const card = $("status-card"), i = S.index || {};
    const name = i.folder ? i.folder.split("/").filter(Boolean).pop() : "";
    let cls = "idle", h = "索引已是最新", d = "每周自动更新";
    if (i.state === "running") {
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

  // ---------------- 问答区 ----------------
  function modelBanner() {
    const bad = S.models.filter((m) => !m.ok);
    if (S.bootError) return `<div class="banner" role="alert">启动出错：${esc(S.bootError)}</div>`;
    if (!bad.length) return "";
    return `<div class="banner" role="alert"><div><strong>模型没有就绪</strong>：${bad.map((m) => esc(m.ref)).join("、")}。<br>请先打开 Ollama 应用；如果缺模型，在终端运行 <code>ollama pull 模型名</code>，然后重新打开本应用。</div></div>`;
  }

  function answerHTML(text, streaming) {
    let h = esc(text).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    h = h.replace(/\[(\d+(?:\s*[,，、]\s*\d+)*)\]/g, (_, nums) =>
      nums.split(/\s*[,，、]\s*/).map((n) => `<button type="button" class="ref${Number(n) === S.sel ? " on" : ""}" data-ref="${n}" aria-label="出处 ${n}">${n}</button>`).join(""));
    return h + (streaming ? '<span class="caret" aria-hidden="true"></span>' : "");
  }

  function foundPills(sources) {
    const files = [];
    for (const s of sources) if (!files.includes(s.file)) files.push(s.file);
    return `<div class="found"><span>找到 ${files.length} 份资料</span>${files.slice(0, 3).map((f) => `<span class="pill" title="${esc(f)}">${esc(f)}</span>`).join("")}</div>`;
  }

  function questionBlock(q, search) {
    const label = search ? "你搜索" : (/^总结：《/.test(q) ? "全文总结" : "你问");
    return `<div><div class="q-label">${label}</div><h1 class="q-title">${esc(q)}</h1></div>`;
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
    h += questionBlock(c.question, c.phase === "search");
    if (c.phase === "search" && c.autoKeyword) {
      h += `<div class="notice"><div><div class="h">这看起来是关键词，已直接搜索原文</div>
        <div class="d">想让模型回答，请输入完整的问题，例如“${esc(c.question)} 在运营商业务里有哪些应用？”</div>
        <div class="chips" style="margin-top:8px"><button type="button" class="btn small" data-action="ask-anyway">仍然让模型回答</button></div></div></div>`;
    }
    if (c.phase === "search") {
      h += `<div class="found"><span>${c.results.length ? `找到 ${c.results.length} 个相关片段 · ${sec(c.ms)}` : "没有找到相关内容"}</span></div>`;
      h += `<div class="card-list">${c.results.map((r) => hitCard(r)).join("")}</div>`;
    } else if (c.kind === "summary" && (c.phase === "retrieving" || c.phase === "reading")) {
      const p = c.progress || {};
      const parts = c.sources.length;
      const mapping = p.stage === "map", final = p.stage === "final";
      const step = (cls, label, sub) => `<li class="${cls}"><span class="ic" aria-hidden="true">${cls === "done" ? checkSvg() : ""}</span>
        <span class="label">${label}</span>${sub ? `<span class="sub">${sub}</span>` : ""}</li>`;
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
      h += c.record.not_found ? notFoundHTML(c.record) : doneHTML(c.record);
    }
    col.innerHTML = h;
  }

  function checkSvg() {
    return '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 6.2l2.3 2.3 4.7-5"/></svg>';
  }

  function doneHTML(r) {
    const isSummary = /^总结：《/.test(r.question);
    let h = !r.context.length ? "" : isSummary
      ? `<div class="found"><span>按 ${r.context.length} 个部分汇总 · 点编号查看对应原文</span></div>` : foundPills(r.context);
    h += `<div class="answer">${answerHTML(r.answer, false)}</div>`;
    const parts = [];
    if (r.ttft_ms != null) parts.push("首字 " + sec(r.ttft_ms));
    if (r.latency_ms != null) parts.push("共 " + sec(r.latency_ms));
    if (r.model) parts.push(esc(r.model.replace(/^local\//, "")) + "（本机）");
    if (!r.cited.length) parts.push("回答没有标注出处，请在右侧核对原文");
    h += `<div class="meta"><span>${parts.join(" · ")}</span><span class="grow"></span>
      <button type="button" class="btn small" data-action="copy">复制回答</button>
      <button type="button" class="btn small" data-action="again">重新回答</button></div>`;
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
          <div class="body">${highlight(s.text, answer, s.n)}</div>
          <div class="acts">
            <button type="button" class="btn primary" data-open="${esc(s.path)}">${s.type === "pdf" && s.page ? `打开（第 ${s.page} 页）` : "打开原文件"}</button>
            <button type="button" class="btn" data-reveal="${esc(s.path)}">在访达中显示</button>
            <button type="button" class="btn" data-summarize="${esc(s.path)}">总结这份文件</button>
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
  async function submit(text, forceAsk) {
    const q = (text != null ? text : $("q").value).trim();
    if (!q) return;
    if (S.view !== "qa") showView("qa");
    $("q").value = "";
    const autoKeyword = S.mode === "ask" && !forceAsk && isKeywordQuery(q);
    if (S.mode === "search" || autoKeyword) {
      S.cur = { phase: "search", question: q, results: [], ms: 0, autoKeyword };
      try {
        const r = await api("search", q);
        S.cur.results = r.results; S.cur.ms = r.ms;
        S.sel = r.results.length ? 1 : null;
      } catch (e) { S.cur = { phase: "error", question: q, error: String(e.message || e) }; }
      renderQA(); renderAside(); renderSide();
      return;
    }
    S.cur = { phase: "retrieving", question: q, text: "", sources: [] };
    S.sel = null;
    renderQA(); renderAside(); renderSide();
    setGenerating(true);
    try {
      const r = await api("ask", q);
      if (r.error) { S.cur.phase = "error"; S.cur.error = r.error; setGenerating(false); renderQA(); return; }
      S.cur.qid = r.qid;
    } catch (e) {
      S.cur.phase = "error"; S.cur.error = String(e.message || e); setGenerating(false); renderQA();
    }
    clearInterval(ticker);
    ticker = setInterval(() => {
      const w = $("waited");
      if (!S.cur || S.cur.phase !== "reading") { if (!S.cur || S.cur.phase !== "retrieving") clearInterval(ticker); return; }
      if (w) w.textContent = `已等待 ${Math.floor((Date.now() - S.cur.readingSince) / 1000)} 秒 · 通常 5–10 秒后开始出字`;
    }, 1000);
  }

  async function summarizeFile(path) {
    if (!$("generating").hidden) { toast("正在回答，请稍等或先停止"); return; }
    if (S.view !== "qa") showView("qa");
    const name = path.split("/").pop();
    S.cur = { phase: "retrieving", kind: "summary", question: `总结：《${name}》`, text: "", sources: [] };
    S.sel = null;
    renderQA(); renderAside(); renderSide();
    setGenerating(true);
    try {
      const r = await api("summarize", path);
      if (r.error) { S.cur.phase = "error"; S.cur.error = r.error; setGenerating(false); renderQA(); return; }
      S.cur.qid = r.qid;
    } catch (e) {
      S.cur.phase = "error"; S.cur.error = String(e.message || e); setGenerating(false); renderQA();
    }
  }

  function setMode(mode) {
    S.mode = mode;
    document.querySelectorAll(".seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
    $("q").placeholder = mode === "search" ? "输入关键词，只搜索原文（不生成回答，很快）" : "问一个关于你资料的问题，⌘↩ 发送";
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
    S.cur = { phase: "done", record: r, question: r.question };
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
      if (active) state = `<span class="state" style="color:var(--run-ink)">${s.last_indexed ? "更新中" : "首次索引中"}</span>`;
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
          <span>${last ? "上次更新 " + esc(last) : "下次打开或空闲时继续"}</span><span>每周自动更新</span></div>`;
      }
      const confirming = S.confirmRemove === s.id;
      h += `<div class="row" style="align-items:center">
        <span class="grow"></span>
        ${active ? (i.state === "paused" && i.reason === "manual" ? '<button type="button" class="btn small" data-action="resume">继续</button>' : '<button type="button" class="btn small" data-action="pause">暂停</button>') + '<button type="button" class="btn small" data-action="stop-index">停止</button>'
          : `<button type="button" class="btn small" data-update="${esc(s.path)}">立即更新</button>`}
        ${confirming ? `<span class="sub">只删除索引，不动原文件</span><button type="button" class="btn small danger" data-remove-yes="${s.id}">确认移除</button><button type="button" class="btn small" data-action="remove-no">取消</button>`
          : `<button type="button" class="btn small danger" data-remove="${s.id}">移除</button>`}
        </div></section>`;
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
    if (d.copySrc) { const s = currentSources().find((x) => x.n === Number(d.copySrc)); if (s) copy(s.text); return; }
    if (d.update) { await api("update_source", d.update); toast("已加入更新队列"); return; }
    if (d.remove) { S.confirmRemove = Number(d.remove); return renderLib(); }
    if (d.removeYes) {
      S.confirmRemove = null; toast("正在移除…");
      const r = await api("remove_source", Number(d.removeYes));
      toast(r.error || `已移除 ${fmt(r.removed_files)} 个文件的索引`); return refreshLibrary();
    }
    switch (d.action) {
      case "add": return addFolder();
      case "lib": return showView("lib");
      case "copy": return copy(S.cur.record.answer.replace(/\s*\[\d+(?:\s*[,，、]\s*\d+)*\]/g, ""));
      case "again":
        if (S.cur.record && /^总结：《/.test(S.cur.question) && S.cur.record.context.length) return summarizeFile(S.cur.record.context[0].path);
        return submit(S.cur.question, true);
      case "ask-anyway": return submit(S.cur.question, true);
      case "search-instead": setMode("search"); $("q").value = S.cur ? S.cur.question : ""; return;
      case "update-all": { const r = await api("update_all"); toast(r.queued ? `${r.queued} 个文件夹已加入更新队列` : "已经在更新了"); return; }
      case "pause": await api("pause_index"); return refreshLibrary();
      case "resume": await api("resume_index"); return refreshLibrary();
      case "stop-index": await api("stop_index"); toast("已停止，下次空闲时或点“立即更新”继续"); return;
      case "remove-no": S.confirmRemove = null; return renderLib();
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
  document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); submit(); });
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
