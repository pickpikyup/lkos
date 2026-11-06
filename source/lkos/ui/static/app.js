/* Local Knowledge OS 应用界面。通过 window.pywebview.api 调用 Python（lkos/ui/bridge.py），
   Python 通过 window.lkos.onEvent(事件) 推送问答进度与索引进度。 */
(function () {
  "use strict";

  const S = {
    view: "qa", mode: "chat", models: [], model: "", history: [], sources: [], index: { state: "idle" },
    paused: false, cur: null, sel: null, lib: null, confirmRemove: null, problemsCount: 0, libEdit: null,
    settings: { dev_mode: false },
    scope: { source_ids: [], file_types: [], days: 0, path: null, tags: [] }, scopeOpen: false, tags: [],
    thread: null, selTurn: null, choices: [], modelRef: "", attach: [], picker: null, autoKeyword: {},
    mcfg: null, modelEdit: null, modelList: null, consentOk: null,
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
        c.info = Object.assign(c.info || {}, e.detail || {});
        if (c.phase === "retrieving") c.phase = "reading";
        renderQA();
      } else if (e.type === "sources") {
        c.sources = e.sources; c.retrievalMs = e.ms; c.phase = "reading"; c.readingSince = Date.now();
        S.selTurn = "cur"; S.sel = e.sources.length ? 1 : null;
        renderQA(); renderAside();
      } else if (e.type === "confirm_send") {
        c.confirm = { items: e.items, keep: new Set(e.items.map((x) => x.n)), excluded: e.excluded, turns: e.turns,
                      model: e.model, service: e.service };
        renderConfirm();
      } else if (e.type === "token") {
        if (c.phase !== "streaming") { c.phase = "streaming"; c.confirm = null; renderConfirm(); renderQA(); }
        c.text += e.text;
        if (c.kind === "analysis") updateBar(c);
        const el = $("answer-live");
        if (el) el.innerHTML = answerHTML(c.text, true, "cur", c.kind === "chat");
        keepBottom();
      } else if (e.type === "done") {
        S.cur = null;
        appendTurn(e.record);
        loadHistory();
        setGenerating(false); renderConfirm();
        renderQA(); renderAside();
        const turnEl = document.querySelector(".turn:last-of-type");
        if (turnEl) turnEl.scrollIntoView({ block: "start", behavior: "smooth" });
      } else if (e.type === "stopped") {
        c.phase = "stopped"; c.confirm = null; setGenerating(false); renderConfirm(); renderQA();
      } else if (e.type === "error") {
        c.phase = "error"; c.error = e.message; c.confirm = null; setGenerating(false); renderConfirm(); renderQA();
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
      S.stats = b.stats; S.settings = b.settings || S.settings; setFileTypes(b.file_types);
      S.choices = b.choices || [];
    } catch (err) {
      S.bootError = String(err && err.message || err);
    }
    applyDevMode();
    try {
      S.modelRef = localStorage.getItem("lkos.model") || "";
      const m = localStorage.getItem("lkos.mode");
      if (m === "chat" || m === "search" || m === "qa" || m === "analysis") S.mode = m;
    } catch (e) { /* 忽略 */ }
    setMode(S.mode);
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
    const curTid = S.thread && S.thread.id;
    let html = "", last = null;
    if (!S.history.length) html = '<div class="side-label">对话</div><div class="side-empty">提过的问题会出现在这里</div>';
    for (const h of S.history.slice(0, 80)) {
      const g = dayLabel(h.asked_at);
      if (g !== last) { html += `<div class="side-label">${g}</div>`; last = g; }
      const title = h.question.replace(/^(分析|提取)：/, "");
      html += `<button type="button" class="side-item${h.thread_id === curTid && S.view === "qa" ? " active" : ""}" data-thread="${h.thread_id}" title="${esc(title)}${h.branch_from ? `（分支自：${esc(h.branch_from)}）` : ""}">
        ${h.branch_of ? '<svg class="br" aria-label="分支" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="4" cy="3.5" r="1.5"/><circle cx="4" cy="12.5" r="1.5"/><circle cx="12" cy="6" r="1.5"/><path d="M4 5v6M12 7.5c0 2.5-8 1.5-8 3.5"/></svg>' : ""}
        <span class="t">${esc(title)}</span>${h.turns > 1 ? `<span class="n">${h.turns}</span>` : ""}</button>`;
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
  // 文件类型由 Python 端的解析器注册表生成（boot 返回 file_types），新增格式时这里自动出现
  let TYPES = [], SKIP_TYPES = [], FORMATS = "";
  function setFileTypes(groups) {
    const list = groups && groups.length ? groups : [{ key: "pdf", label: "PDF", types: ["pdf"], exts: ["pdf"], indexable: true }];
    TYPES = list.filter((g) => g.indexable).map((g) => [g.key, g.label, g.types]);
    SKIP_TYPES = list.map((g) => [g.key, g.label, g.exts]);
    FORMATS = list.filter((g) => g.indexable).map((g) => (g.skipped_exts || []).length
      ? `${g.label}（${g.exts.filter((e) => !g.skipped_exts.includes(e)).map((e) => "." + e).join(" / ")}）` : g.label).join("、");
    const drop = $("drop-formats");
    if (drop) drop.textContent = FORMATS;
  }
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

  // ---------------- 对话区 ----------------
  function modelBanner() {
    const bad = S.models.filter((m) => !m.ok);
    if (S.bootError) return `<div class="banner" role="alert">启动出错：${esc(S.bootError)}</div>`;
    if (!bad.length) return "";
    return `<div class="banner" role="alert"><div><strong>本机模型没有就绪</strong>：${bad.map((m) => esc(m.ref)).join("、")}。<br>请先打开 Ollama 应用；如果缺模型，在终端运行 <code>ollama pull 模型名</code>，然后重新打开本应用。也可以在左栏“模型”里换用别的模型。</div></div>`;
  }

  function answerHTML(text, streaming, turn, plain) {
    let h = esc(text).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    h = h.replace(/^#{2,4}\s*(.+)(?:\n|$)/gm, '<span class="ah">$1</span>');
    if (plain) return h + (streaming ? '<span class="caret" aria-hidden="true"></span>' : "");   // 对话模式没有出处编号
    const on = (n) => String(S.selTurn) === String(turn) && Number(n) === S.sel;
    h = h.replace(/\[(\d+(?:\s*[,，、]\s*\d+)*)\]/g, (_, nums) =>
      nums.split(/\s*[,，、]\s*/).map((n) => `<button type="button" class="ref${on(n) ? " on" : ""}" data-ref="${n}" data-turn="${turn}" aria-label="出处 ${n}">${n}</button>`).join(""));
    return h + (streaming ? '<span class="caret" aria-hidden="true"></span>' : "");
  }

  function foundPills(sources) {
    const files = [];
    for (const s of sources) if (!files.includes(s.file)) files.push(s.file);
    return `<div class="found"><span>依据 ${files.length} 份资料</span>${files.slice(0, 3).map((f) => `<span class="pill" title="${esc(f)}">${esc(f)}</span>`).join("")}</div>`;
  }

  const KIND_LABEL = { chat: "对话", qa: "分析", search: "关键词检索", analysis: "深度分析", summary: "全文总结", extract: "信息提取" };
  function questionBlock(kind, question, scope, attachments) {
    const title = String(question).replace(/^(分析|提取)：/, "");
    const notes = [];
    if (scope && Object.keys(scope).length) notes.push("范围：" + esc(scopeText(scope)));
    if (attachments && attachments.length) notes.push(`附加 ${attachments.length} 个文件`);
    return `<div class="q"><div class="q-label">${KIND_LABEL[kind] || "分析"}${notes.length ? `<span class="q-scope"> · ${notes.join(" · ")}</span>` : ""}</div>
      <h2 class="q-title">${esc(title)}</h2></div>`;
  }

  function step(cls, label, sub) {
    return `<li class="${cls}"><span class="ic" aria-hidden="true">${cls === "done" ? checkSvg() : ""}</span>
      <span class="label">${label}</span>${sub ? `<span class="sub">${sub}</span>` : ""}</li>`;
  }
  const SKELETON = '<div class="skeleton" aria-hidden="true"><div style="width:92%"></div><div style="width:78%"></div><div style="width:54%"></div></div>';
  const LANG_NAME = { zh: "中文", en: "English", fr: "Français" };

  // 深度分析的总进度（估算）：拆分 0–8%，关键词 8–16%，检索 16–35%，模型读资料 35–45%，写分析 45–100%（按已写字数）
  function analysisPct(c) {
    const p = c.progress || { stage: "plan", done: 0, total: 1 }, frac = p.total ? p.done / p.total : 0;
    if (c.phase === "streaming") return Math.min(99, 45 + 55 * Math.min(1, (c.text || "").length / 1100));
    if (p.stage === "plan") return 8 * frac;
    if (p.stage === "keywords") return 8 + 8 * frac;
    if (p.stage === "search" && p.done < p.total) return 16 + 19 * frac;
    const waited = c.writeSince ? (Date.now() - c.writeSince) / 1000 : 0;
    return 35 + 10 * Math.min(1, waited / 40);
  }
  function barHTML(c) {
    const pct = Math.round(analysisPct(c));
    return `<div class="pbar" role="progressbar" aria-label="深度分析进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}">
      <div class="track"><div class="fill" id="pbar-fill" style="width:${pct}%"></div></div><span class="pct" id="pbar-pct">${pct}%</span></div>`;
  }
  function updateBar(c) {
    const pct = Math.round(analysisPct(c)), f = $("pbar-fill"), t = $("pbar-pct");
    if (f) f.style.width = pct + "%";
    if (t) t.textContent = pct + "%";
    const bar = f && f.closest(".pbar");
    if (bar) bar.setAttribute("aria-valuenow", String(pct));
  }
  function keywordPills(kw, hits) {
    const langs = ["zh", "en", "fr"].filter((l) => kw && kw[l]);
    if (!langs.length) return "";
    return `<div class="kwlist">${langs.map((l) => `<div class="kw"><span class="lang">${LANG_NAME[l]}</span><span class="words">${esc(kw[l])}</span>${hits && hits[l] != null ? `<span class="n">${hits[l] ? `找到 ${hits[l]} 段` : "没找到"}</span>` : ""}</div>`).join("")}</div>`;
  }

  function analysisSteps(c) {
    const p = c.progress || { stage: "plan", done: 0, total: 1 }, info = c.info || {};
    const order = ["plan", "keywords", "search", "write"];
    let at = order.indexOf(p.stage);
    if (p.stage === "search" && p.done >= p.total) at = 3;
    if (at === 3 && !c.writeSince) c.writeSince = Date.now();
    const st = (i) => (i < at ? "done" : i === at ? "now" : "");
    const aspects = (info.aspects || []).length ? `拆成 ${info.aspects.length} 个方面：${info.aspects.map(esc).join("；")}` : "通常 5–15 秒";
    const kwSub = info.keywords ? (Object.keys(info.keywords).length ? "" : "模型没给出关键词，只按原问题检索") : (at === 1 ? "通常 5–10 秒" : "");
    const searchSub = p.stage === "search" && p.done < p.total && p.detail && p.detail.current ? `正在查：${esc(p.detail.current)}`
      : (at > 2 && c.sources.length ? `送入 ${c.sources.length} 段（${new Set(c.sources.map((s) => s.file)).size} 份资料），右侧可以先看` : "");
    return `${barHTML(c)}<ol class="steps" aria-label="进度">
      ${step(st(0), "把问题拆成几个要分别查的方面", at === 0 ? (c.online ? "在线模型通常几秒" : "通常 5–15 秒") : aspects)}
      ${step(st(1), "把关键词写成中文、英文、法文", kwSub)}
      ${at >= 1 && info.keywords ? `<li class="kwrow">${keywordPills(info.keywords, info.lang_hits)}</li>` : ""}
      ${step(st(2), `按方面、按语言查找全部资料${p.stage === "search" ? `（${Math.min(p.done, p.total)} / ${p.total}）` : ""}`, searchSub)}
      ${step(st(3), "按方面写分析、综合结论并标注出处", at === 3 ? (c.online ? "" : "需要读的资料较多，通常 20–40 秒后开始出字") : "")}</ol>${SKELETON}`;
  }

  function checkSvg() {
    return '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 6.2l2.3 2.3 4.7-5"/></svg>';
  }

  // 每个回答底部：哪个模型回答的（本机 / 在线）
  function modelTag(name, online) {
    if (!name) return "";
    return `<span class="mtag${online ? " online" : ""}">${esc(name)}<span class="where">${online ? "在线" : "本机"}</span></span>`;
  }

  function welcomeHTML() {
    const st = S.stats || { files: 0, chunks: 0 };
    if (!S.sources.length) {
      return `<div class="welcome"><h1>先添加资料文件夹</h1>
        <p>选一个放着 ${esc(FORMATS || "PDF、Word、PowerPoint、Markdown")} 文件的文件夹，建好索引后就可以直接提问。文档内容不会离开这台电脑。</p>
        <button type="button" class="btn primary" data-action="add">添加文件夹</button></div>`;
    }
    return `<div class="welcome"><h1>问你的资料</h1>
      <p>已索引 ${fmt(st.files)} 个文件、${fmt(st.chunks)} 个文本块。四种方式：</p>
      <ul class="modes-help">
        <li><strong>对话</strong>：直接和模型聊，不查资料库。</li>
        <li><strong>关键词检索</strong>：按原文逐字查找，列出文件和位置，不用模型，最快。</li>
        <li><strong>分析</strong>：理解问题后找资料，用文档写出带出处的回答。</li>
        <li><strong>深度分析</strong>：拆解问题、把关键词译成中英法分别检索全部资料，再按方面分析。</li>
      </ul>
      <p class="hint">回答后直接接着问就是追问；想换话题按 ⌘N。</p></div>`;
  }

  function renderQA() {
    const col = $("qa-col");
    let h = modelBanner();
    const turns = S.thread ? S.thread.turns : [];
    if (!turns.length && !S.cur) {
      col.innerHTML = h + welcomeHTML();
      renderComposerTop();
      return;
    }
    if (S.thread && turns.length && turns[0].branch_of) {
      h += `<div class="branch-note">分支对话：保留了原对话到这一轮为止的内容，之后的提问只在这里继续。</div>`;
    }
    for (const r of turns) h += `<article class="turn${String(S.selTurn) === String(r.id) ? " sel" : ""}" data-turn-id="${r.id}">${turnHTML(r)}</article>`;
    if (S.cur) h += `<article class="turn current${S.selTurn === "cur" ? " sel" : ""}">${curHTML(S.cur)}</article>`;
    col.innerHTML = h;
    renderComposerTop();
  }

  function turnHTML(r) {
    const ex = r.extra || {};
    let h = questionBlock(r.kind, r.question, ex.scope, ex.attachments);
    if (r.kind === "search") return h + searchHTML(r);
    if (r.kind === "chat") return h + `<div class="answer">${answerHTML(r.answer, false, r.id, true)}</div>` + metaHTML(r);
    if (r.not_found) return h + notFoundHTML(r);
    if (r.context.length) h += foundPills(r.context);
    if (r.kind === "analysis" && (ex.aspects || []).length) {
      h += `<div class="aspects"><span>分 ${ex.aspects.length} 个方面分析：</span>${ex.aspects.map((a) => `<span class="pill">${esc(a)}</span>`).join("")}</div>`;
    }
    if (r.kind === "analysis" && ex.keywords && Object.keys(ex.keywords).length) {
      h += `<details class="kwdone"><summary>三种语言的检索关键词</summary>${keywordPills(ex.keywords, ex.lang_hits)}</details>`;
    }
    h += `<div class="answer">${answerHTML(r.answer, false, r.id)}</div>`;
    return h + metaHTML(r);
  }

  function metaHTML(r) {
    const ex = r.extra || {}, parts = [];
    if (r.ttft_ms != null && r.kind !== "search") parts.push("首字 " + sec(r.ttft_ms));
    if (r.latency_ms != null) parts.push("共 " + sec(r.latency_ms));
    if (ex.sent && r.kind === "chat") parts.push(ex.sent.turns ? `发送了 ${ex.sent.turns} 轮对话记录` : "");
    else if (ex.sent) parts.push(`发送了 ${ex.sent.chunks} 段资料${ex.sent.turns ? `和 ${ex.sent.turns} 轮对话` : ""}`);
    if (r.kind !== "search" && r.kind !== "chat" && !r.not_found && !r.cited.length) parts.push("没有标注出处，请在右侧核对原文");
    return `<div class="meta">${r.kind === "search" ? "" : modelTag(r.model_name, r.online)}<span>${parts.join(" · ")}</span><span class="grow"></span>
      <button type="button" class="btn small" data-branch="${r.id}" title="保留到这一轮为止的内容，另开一个对话">分支</button>
      ${r.kind === "search" ? "" : `<button type="button" class="btn small" data-copy-turn="${r.id}">复制</button>`}</div>`;
  }

  function hlTerms(text, terms) {
    let h = esc(text);
    const list = (terms || []).filter(Boolean).sort((a, b) => b.length - a.length);
    if (!list.length) return h;
    const re = new RegExp(list.map((t) => esc(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "gi");
    return h.replace(re, (m) => `<mark>${m}</mark>`);
  }

  function searchHTML(r) {
    const res = (r.extra || {}).results || { files: [], terms: [] };
    let h = "";
    if (S.autoKeyword && S.autoKeyword[r.id]) {
      h += `<div class="notice"><div><div class="h">这看起来是关键词，已直接按原文查找</div>
        <div class="d">想让模型回答，可以输入完整的问题，例如“${esc(r.question)} 在业务里有哪些应用？”</div>
        <div class="chips" style="margin-top:8px"><button type="button" class="btn small" data-ask-anyway="${r.id}">仍然让模型分析</button></div></div></div>`;
    }
    if (!res.files.length) {
      return h + `<div class="found"><span>没有找到同时包含 ${res.terms.map((t) => `“${esc(t)}”`).join("、")} 的文件 · ${sec(res.ms)}</span></div>
        <div class="hint">关键词检索按原文逐字匹配；可以减少关键词、换个说法，或改用“分析”让模型理解问题。</div>`;
    }
    const more = res.file_count > res.files.length ? `（显示前 ${res.files.length} 个）` : "";
    h += `<div class="found"><span>找到 ${fmt(res.total_hits)} 处，在 ${fmt(res.file_count || res.files.length)} 个文件里${more} · ${sec(res.ms)}</span></div><div class="kfiles">`;
    res.files.forEach((f, i) => {
      const on = String(S.selTurn) === String(r.id) && S.sel === i + 1;
      h += `<button type="button" class="kfile${on ? " on" : ""}" data-kfile="${i + 1}" data-turn="${r.id}">
        <div class="top"><span class="name">${esc(f.name)}</span><span class="cnt">${fmt(f.count)} 处</span></div>
        <div class="dir" title="${esc(f.folder)}">${esc(f.folder)}</div>
        ${f.hits.slice(0, 3).map((x) => `<div class="khit"><span class="loc">${esc(x.loc || "位置")}</span><span class="snip">${hlTerms(x.snippet, res.terms)}</span></div>`).join("")}
        ${f.hits.length > 3 ? `<div class="more">还有 ${f.hits.length - 3} 处，点开在右侧查看</div>` : ""}</button>`;
    });
    return h + "</div>" + metaHTML(r);
  }

  function curHTML(c) {
    let h = questionBlock(c.kind, c.question, c.scope, c.attachments);
    if (c.kind === "chat" && (c.phase === "retrieving" || c.phase === "reading")) {
      h += `<div class="thinking"><span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>${esc(c.model_name || "模型")}正在思考${c.online ? "" : "（本机模型通常几秒后开始出字）"}</div>`;
    } else if (c.kind === "analysis" && (c.phase === "retrieving" || c.phase === "reading")) {
      h += analysisSteps(c);
    } else if (c.phase === "retrieving" || c.phase === "reading") {
      const reading = c.phase === "reading";
      const waited = reading ? Math.floor((Date.now() - c.readingSince) / 1000) : 0;
      const waitHint = c.confirm ? "等你确认要发送的资料" : (c.online ? "在线模型通常几秒内开始出字" : "通常 5–10 秒后开始出字");
      h += `<ol class="steps" aria-label="进度">
        ${step(reading ? "done" : "now", reading ? `已找到 ${new Set(c.sources.map((s) => s.file)).size} 份相关资料` : "正在本机查找资料", reading ? `${sec(c.retrievalMs)} · 右侧可以先看` : "")}
        <li class="${reading ? "now" : ""}"><span class="ic" aria-hidden="true"></span><span class="label">${esc(c.model_name || "模型")}正在阅读资料</span>
          ${reading ? `<span class="sub" id="waited">已等待 ${waited} 秒 · ${waitHint}</span>` : ""}</li>
        <li><span class="ic" aria-hidden="true"></span><span class="label">生成回答并标注出处</span></li></ol>${SKELETON}`;
    } else if (c.phase === "streaming") {
      if (c.kind === "analysis") h += barHTML(c);
      if (c.sources && c.sources.length) h += foundPills(c.sources);
      h += `<div class="answer" id="answer-live" aria-live="polite">${answerHTML(c.text, true, "cur", c.kind === "chat")}</div>
        <div class="meta">${modelTag(c.model_name, c.online)}</div>`;
    } else if (c.phase === "stopped") {
      h += `<div class="answer">${answerHTML(c.text || "", false, "cur")}</div><div class="meta">${modelTag(c.model_name, c.online)}<span>${c.text ? "已停止生成（这一轮没有保存）" : "已取消"}</span></div>`;
    } else if (c.phase === "error") {
      h += `<div class="banner" role="alert">${esc(c.error)}</div>`;
    }
    return h;
  }

  function notFoundHTML(r) {
    let h = `<div class="notice"><svg aria-hidden="true" width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="9" cy="9" r="5.5"/><path d="M13.2 13.2L17 17"/></svg>
      <div><div class="h">资料里没有找到这个问题的答案</div>
      <div class="d">为了不编造，模型不会猜测。${r.context.length ? "右侧是最接近的片段，可能与问题无关，请自己判断。" : ""}
      ${(r.extra || {}).sent && !(r.extra.sent.chunks) && r.extra.sent.excluded ? "找到的资料所在的文件夹设置为不发送到在线模型，可以改用本机模型。" : ""}</div></div></div>
      <div class="chips"><button type="button" class="btn small" data-search-instead="${r.id}">改用关键词检索</button>
      ${S.problemsCount ? `<button type="button" class="btn small" data-action="lib">有 ${S.problemsCount} 个文件没能索引，查看</button>` : ""}</div>`;
    return h + metaHTML(r);
  }

  // ---------------- 右栏：出处 ----------------
  function selTurnRecord() {
    if (S.selTurn === "cur") return null;
    return S.thread && S.thread.turns.find((t) => String(t.id) === String(S.selTurn));
  }
  function currentSources() {
    if (S.selTurn === "cur" && S.cur) return S.cur.sources || [];
    const r = selTurnRecord();
    return r && r.kind !== "search" ? r.context : [];
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

  function srcActions(path, type, page, n) {
    return `<div class="acts">
      <button type="button" class="btn primary" data-open="${esc(path)}">${type === "pdf" && page ? `打开（第 ${page} 页）` : "打开原文件"}</button>
      <button type="button" class="btn" data-reveal="${esc(path)}">在访达中显示</button>
      <button type="button" class="btn" data-scope-file="${esc(path)}">只问这份文件</button>
      <button type="button" class="btn" data-attach="${esc(path)}">附加到对话</button>
      ${n != null ? `<button type="button" class="btn" data-copy-src="${n}">复制原文</button>` : ""}
    </div>`;
  }

  function renderAside() {
    const a = $("aside");
    const r = selTurnRecord();
    if (r && r.kind === "search") return renderSearchAside(a, r);
    if ((r && r.kind === "chat") || (S.selTurn === "cur" && S.cur && S.cur.kind === "chat")) {
      a.innerHTML = `<h2>出处</h2><div class="aside-empty">对话模式不查资料库，回答来自模型本身的知识，没有出处。想依据你的资料回答，切换到“分析”。</div>`;
      return;
    }
    const list = currentSources();
    if (!list.length) {
      const loading = S.selTurn === "cur" && S.cur && S.cur.phase === "retrieving";
      a.innerHTML = `<h2>出处</h2><div class="aside-empty">${loading ? "正在查找资料…" : "这里显示回答依据的原文。点回答里的编号可以切换；点前面某一轮的编号，显示那一轮的出处。"}</div>`;
      return;
    }
    const cur = S.selTurn === "cur";
    const answer = cur ? (S.cur.text || "") : r.answer;
    const turns = S.thread ? S.thread.turns : [];
    const idx = cur ? turns.length + 1 : turns.findIndex((t) => t.id === r.id) + 1;
    let h = `<div><h2>${cur ? "已找到的资料" : "出处"}</h2><div class="sub">${turns.length + (S.cur ? 1 : 0) > 1 ? `第 ${idx} 轮 · ` : ""}${cur ? "模型正在阅读这些片段" : "点回答里的编号切换"}</div></div>`;
    for (const s of list) {
      if (s.n === S.sel) {
        h += `<div class="src on" aria-current="true">
          <div class="top"><span class="num">[${s.n}]</span><span class="name">${esc(s.file)}</span></div>
          ${s.loc ? `<div class="loc">${esc(s.loc)}</div>` : ""}
          <div class="src-tags" id="src-tags" data-path="${esc(s.path)}"></div>
          <div class="body">${highlight(s.text, answer, s.n)}</div>
          ${srcActions(s.path, s.type, s.page, s.n)}</div>`;
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

  function renderSearchAside(a, r) {
    const res = r.extra.results || { files: [], terms: [] };
    const f = res.files[(S.sel || 1) - 1];
    if (!f) { a.innerHTML = `<h2>检索结果</h2><div class="aside-empty">点左边的文件，在这里查看每一处出现的位置。</div>`; return; }
    a.innerHTML = `<div><h2>${esc(f.name)}</h2><div class="sub" title="${esc(f.folder)}">${esc(f.folder)} · ${fmt(f.count)} 处</div></div>
      <div class="src-tags" id="src-tags" data-path="${esc(f.path)}"></div>
      ${f.hits.map((x) => `<div class="src khit-full"><div class="loc">${esc(x.loc || "位置")}</div><div class="body">${hlTerms(x.snippet, res.terms)}</div>
        ${x.page && f.type === "pdf" ? `<div class="acts"><button type="button" class="btn small" data-open="${esc(f.path)}">打开（第 ${x.page} 页）</button></div>` : ""}</div>`).join("")}
      ${f.hits.length < f.count ? `<div class="hint">只列出前 ${f.hits.length} 处</div>` : ""}
      ${srcActions(f.path, f.type, null, null)}`;
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

  function select(n, turn) {
    if (turn != null) S.selTurn = turn === "cur" ? "cur" : Number(turn);
    S.sel = Number(n);
    renderAside(); renderQA();
    const on = $("aside").querySelector(".src.on");
    if (on) on.scrollIntoView({ block: "nearest" });
  }

  function selectTurn(r) {
    S.selTurn = r.id;
    S.sel = r.kind === "search" ? (r.extra.results && r.extra.results.files.length ? 1 : null)
      : (r.cited.length ? r.cited[0] : (r.context.length ? 1 : null));
  }

  function appendTurn(r) {
    if (!S.thread || S.thread.id !== r.thread_id) S.thread = { id: r.thread_id, turns: [] };
    S.thread.turns.push(r);
    selectTurn(r);
  }

  // ---------------- 输入区：模式、模型、附件、确认发送 ----------------
  function setGenerating(on) {
    $("composer").hidden = on;
    $("generating").hidden = !on;
    if (!on) $("q").focus();
  }

  // 只有一两个关键词、没有问句特征的输入（如“AI”“5G FWA”），直接按原文查找
  const QUESTION_HINT = /[?？]|什么|怎么|怎样|如何|为什么|为何|哪|谁|多少|几|是否|吗|呢|是不是|有没有|介绍|总结|概括|说明|解释|比较|区别|列出|列举|告诉|分析|\b(what|how|why|who|which|when|where|is|are|does|do|can|list|explain|summari[sz]e|compare)\b/i;
  function isKeywordQuery(q) {
    if (QUESTION_HINT.test(q)) return false;
    const cjk = (q.match(/[㐀-鿿]/g) || []).length;
    const words = (q.match(/[A-Za-z0-9][A-Za-z0-9.+\-]*/g) || []).length;
    return cjk <= 6 && words <= 2 && q.length <= 16;
  }

  function currentChoice() {
    return S.choices.find((c) => c.ref === S.modelRef) || S.choices[0] || null;
  }

  function renderModelSelect() {
    const sel = $("model-select");
    if (!sel) return;
    if (!S.choices.some((c) => c.ref === S.modelRef)) S.modelRef = S.choices.length ? S.choices[0].ref : "";
    const local = S.choices.filter((c) => !c.online), online = S.choices.filter((c) => c.online);
    sel.innerHTML = `<optgroup label="本机（不联网）">${local.map((c) => `<option value="${esc(c.ref)}">${esc(c.name)}</option>`).join("")}</optgroup>` +
      (online.length ? `<optgroup label="在线">${online.map((c) => `<option value="${esc(c.ref)}">${esc(c.name)}</option>`).join("")}</optgroup>` : "") +
      `<option value="__config">配置模型…</option>`;
    sel.value = S.modelRef;
    $("model-wrap").hidden = S.mode === "search";
    $("scope-wrap").hidden = S.mode === "chat";
    $("model-wrap").classList.toggle("online", !!(currentChoice() && currentChoice().online));
  }

  // 输入框上方：继续对话 / 附加的文件 / 在线发送提示
  function renderComposerTop() {
    const top = $("composer-top");
    if (!top) return;
    const parts = [];
    const turns = S.thread ? S.thread.turns : [];
    if (turns.length) {
      const first = turns[0].question.replace(/^(分析|提取)：/, "");
      parts.push(`<div class="ctx-chip"><svg aria-hidden="true" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M3 4h10M3 8h7M3 12h4"/></svg>
        <span>继续对话：${esc(first)} · 已 ${turns.length} 轮</span><button type="button" class="link" data-action="new">新对话 ⌘N</button></div>`);
    }
    if (S.attach.length && S.mode !== "chat") {
      parts.push(`<div class="attach-row">${S.attach.map((f, i) => `<span class="att${S.mode !== "search" && currentChoice() && currentChoice().online && !f.online_ok ? " blocked" : ""}" title="${esc(f.path)}">
        <svg aria-hidden="true" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 2h5l3 3v9H4z"/></svg>${esc(f.name)}
        <span class="tk">约 ${fmt(Math.round(f.tokens / 1000) || 1)}k</span><button type="button" data-unattach="${i}" aria-label="去掉 ${esc(f.name)}">×</button></span>`).join("")}</div>`);
    }
    const c = currentChoice();
    if (S.mode === "chat" && c && c.online) {
      parts.push(`<div class="online-note"><svg aria-hidden="true" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><path d="M2 8h12M8 2c2 2 2 10 0 12M8 2c-2 2-2 10 0 12"/></svg>
        <span>在线：你的问题${turns.length ? "和这个对话的记录" : ""}会发送到 ${esc(c.name)}；对话模式不发送资料库里的内容</span></div>`);
    } else if (S.mode !== "search" && c && c.online) {
      const tk = S.attach.reduce((s, f) => s + f.tokens, 0);
      const over = tk > c.context_window * 0.7;
      parts.push(`<div class="online-note${over ? " warn" : ""}"><svg aria-hidden="true" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><path d="M2 8h12M8 2c2 2 2 10 0 12M8 2c-2 2-2 10 0 12"/></svg>
        <span>在线：本机检索到的资料片段${turns.length ? "、这个对话的记录" : ""}${S.attach.length ? "和附加文件的全文" : ""}会发送到 ${esc(c.name)}${S.settings.confirm_online !== false ? "，发送前可以逐段确认" : ""}${over ? `。附加的文件约 ${fmt(Math.round(tk / 1000))}k token，接近模型上限，可能放不下` : ""}</span></div>`);
    } else if (S.mode !== "search" && S.mode !== "chat" && S.attach.length) {
      parts.push(`<div class="online-note local"><span>本机模型放不下整份文件：只在附加的文件里检索</span></div>`);
    }
    top.innerHTML = parts.join("");
    top.hidden = !parts.length;
  }

  function renderConfirm() {
    const box = $("confirm-send"), c = S.cur;
    const cf = c && c.confirm;
    box.hidden = !cf;
    $("generating").classList.toggle("with-confirm", !!cf);
    if (!cf) { box.innerHTML = ""; return; }
    const n = cf.keep.size;
    box.innerHTML = `<div class="cf-head"><strong>发送到 ${esc(cf.model)}？</strong><span class="hint">${esc(cf.service)}</span></div>
      <div class="cf-sub">勾选要发送的资料片段${cf.turns ? `；这个对话的 ${cf.turns} 轮记录也会一起发送` : ""}${cf.excluded ? `；另有 ${cf.excluded} 段所在文件夹设置为不发送，已排除` : ""}。</div>
      <div class="cf-list">${cf.items.length ? cf.items.map((x) => `<label class="cf-item"><input type="checkbox" data-cf="${x.n}"${cf.keep.has(x.n) ? " checked" : ""}>
        <span class="t"><span class="f">[${x.n}] ${esc(x.file)}${x.loc ? " · " + esc(x.loc) : ""}</span><span class="s">${esc(x.text.replace(/\s+/g, " ").slice(0, 120))}</span></span></label>`).join("")
        : '<div class="hint">没有可发送的资料片段，只会发送问题和对话记录。</div>'}</div>
      <div class="cf-foot"><button type="button" class="btn small" data-cf-all="1">${n === cf.items.length ? "全不选" : "全选"}</button><span class="grow"></span>
        <button type="button" class="btn" data-cf-cancel="1">取消</button>
        <button type="button" class="btn primary" data-cf-send="1">发送 ${n} 段</button></div>`;
  }

  // ---------------- 发送 ----------------
  let ticker = null;
  const GEN_TEXT = { chat: "正在回答", qa: "正在回答，可以先在右侧核对原文", analysis: "深度分析中，可以随时停止" };

  async function submit(text, opts) {
    opts = opts || {};
    const q = (text != null ? text : $("q").value).trim();
    if (!q) return;
    if (!$("generating").hidden) { toast("上一个任务还在进行，请稍等或先停止"); return; }
    if (S.view !== "qa") showView("qa");
    S.scopeOpen = false; renderScope();
    const scope = scopeDict();
    const tid = S.thread ? S.thread.id : null;
    if (S.cur && (S.cur.phase === "error" || S.cur.phase === "stopped")) S.cur = null;
    let mode = opts.mode || (S.mode === "search" ? "search" : S.mode);
    const auto = mode === "qa" && !opts.force && !tid && isKeywordQuery(q);
    if (auto) mode = "search";
    $("q").value = "";
    if (mode === "search") {
      try {
        const r = await api("send", q, "search", tid, null, scope, null);
        if (r.error) { toast(r.error); $("q").value = q; return; }
        if (auto) S.autoKeyword[r.record.id] = true;
        appendTurn(r.record);
        loadHistory();
      } catch (e) { toast(String(e.message || e)); $("q").value = q; return; }
      renderQA(); renderAside();
      $("main-scroll").scrollTop = $("main-scroll").scrollHeight;
      return;
    }
    const choice = currentChoice();
    const ref = opts.model || (choice && choice.ref) || null;
    const ch = S.choices.find((c) => c.ref === ref) || choice;
    const attach = mode === "chat" ? [] : S.attach.map((f) => f.path);
    S.cur = { phase: "retrieving", kind: mode, question: q, text: "", sources: [], scope: mode === "chat" ? null : scope, attachments: attach,
              model_name: ch ? ch.name : "", online: !!(ch && ch.online) };
    S.selTurn = "cur"; S.sel = null;
    $("gen-text").textContent = (ch && ch.online ? `${ch.name}（在线）` : "") + (GEN_TEXT[mode] || GEN_TEXT.qa);
    renderQA(); renderAside(); renderSide();
    $("main-scroll").scrollTop = $("main-scroll").scrollHeight;
    setGenerating(true);
    let r;
    try {
      r = await api("send", q, mode, tid, ref, mode === "chat" ? null : scope, attach);
    } catch (e) { r = { error: String(e.message || e) }; }
    if (r.need_consent) {
      setGenerating(false);
      S.cur = null; renderQA();
      $("q").value = q;
      return showConsent(r.need_consent, () => submit(q, Object.assign({}, opts, { model: ref })));
    }
    if (r.error) { S.cur.phase = "error"; S.cur.error = r.error; setGenerating(false); renderQA(); return; }
    S.cur.qid = r.qid;
    if (mode !== "chat") S.attach = [];
    renderComposerTop();
    clearInterval(ticker);
    ticker = setInterval(() => {
      const c = S.cur;
      if (!c || !["retrieving", "reading", "streaming"].includes(c.phase)) { clearInterval(ticker); return; }
      if (c.kind === "analysis") updateBar(c);
      const w = $("waited");
      if (w && c.phase === "reading" && !c.confirm) w.textContent = `已等待 ${Math.floor((Date.now() - c.readingSince) / 1000)} 秒 · ${c.online ? "在线模型通常几秒内开始出字" : "通常 5–10 秒后开始出字"}`;
    }, 1000);
  }

  function showConsent(info, onOk) {
    const dlg = $("consent");
    dlg.innerHTML = `<div class="dlg" role="dialog" aria-modal="true" aria-labelledby="consent-h">
      <h2 id="consent-h">第一次使用 ${esc(info.name)}</h2>
      <p>用在线模型回答时，会把下面这些发送到 <strong>${esc(info.service)}</strong>：</p>
      <ul><li>你的问题和这个对话之前的问答</li><li>本机检索到的资料片段（不是整份文件；发送前可以逐段确认）</li><li>你主动附加的文件的文字</li></ul>
      <p class="hint">在文件夹设置里关掉“允许发送到在线模型”的文件夹，内容永远不会发送。每次发送都记在日志里。</p>
      <div class="dlg-foot"><button type="button" class="btn" data-consent-no="1">取消</button><button type="button" class="btn primary" data-consent-yes="1">同意并继续</button></div></div>`;
    dlg.hidden = false;
    S.consentOk = async () => { await api("consent_online", info.id); await loadChoices(); onOk(); };
    dlg.querySelector("[data-consent-yes]").focus();
  }

  async function loadChoices() {
    try { S.choices = await api("model_choices"); } catch (e) { /* 忽略 */ }
    renderModelSelect(); renderComposerTop();
  }

  const PLACEHOLDER = {
    chat: "和模型直接对话（不查资料库），⌘↩ 发送",
    search: "输入要查找的词，按原文逐字匹配（空格分开表示都要出现，引号表示整体），⌘↩ 查找",
    qa: "问一个关于你资料的问题；@ 可以附加文件；⌘↩ 发送",
    analysis: "要深入分析的问题，例如“二期项目有哪些主要风险、怎么应对？”；⌘↩ 发送",
  };
  function setMode(mode) {
    S.mode = mode;
    try { localStorage.setItem("lkos.mode", mode); } catch (e) { /* 忽略 */ }
    document.querySelectorAll(".composer .seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
    $("q").placeholder = PLACEHOLDER[mode] || PLACEHOLDER.qa;
    $("attach-btn").hidden = mode === "search" || mode === "chat";
    renderModelSelect(); renderComposerTop();
    $("q").focus();
  }

  function newQuestion() {
    if (!$("generating").hidden) return;
    S.thread = null; S.cur = null; S.sel = null; S.selTurn = null; S.attach = [];
    showView("qa");
    renderQA(); renderAside(); renderSide();
    $("q").focus();
  }

  async function openThread(tid) {
    if (!$("generating").hidden) { toast("正在回答，请稍等或先停止"); return; }
    const turns = await api("thread", Number(tid));
    if (!turns.length) { toast("这个对话已不存在"); loadHistory(); return; }
    S.thread = { id: Number(tid), turns }; S.cur = null;
    selectTurn(turns[turns.length - 1]);
    showView("qa");
    renderQA(); renderAside(); renderSide();
    $("main-scroll").scrollTop = $("main-scroll").scrollHeight;
  }

  async function branchFrom(turnId) {
    if (!$("generating").hidden) { toast("正在回答，请稍等或先停止"); return; }
    const r = await api("branch", Number(turnId));
    if (r.error) { toast(r.error); return; }
    S.thread = { id: r.thread_id, turns: r.turns }; S.cur = null;
    selectTurn(r.turns[r.turns.length - 1]);
    renderQA(); renderAside(); loadHistory();
    toast("已开出分支对话：保留到这一轮为止的内容，接着问就在新对话里");
    $("q").focus();
  }

  // ---------------- 附加文件（📎 / @） ----------------
  let pickTimer = null;
  function openPicker(query, fromAt) {
    S.picker = { query: query || "", fromAt: !!fromAt, items: [], active: 0 };
    renderPicker();
    loadPicker();
  }
  function closePicker() { S.picker = null; renderPicker(); }
  function loadPicker() {
    clearTimeout(pickTimer);
    pickTimer = setTimeout(async () => {
      if (!S.picker) return;
      try { S.picker.items = await api("find_files", S.picker.query, 20); } catch (e) { S.picker.items = []; }
      S.picker.active = 0;
      renderPicker(true);
    }, 120);
  }
  function renderPicker(keepInput) {
    const box = $("picker");
    if (!S.picker) { box.hidden = true; box.innerHTML = ""; return; }
    const p = S.picker, c = currentChoice(), online = c && c.online;
    const list = p.items.map((f, i) => `<button type="button" class="pick${i === p.active ? " on" : ""}" data-pick="${i}" role="option" aria-selected="${i === p.active}">
      <span class="n">${esc(f.name)}</span><span class="d">${esc(f.folder)}</span>
      <span class="tk">约 ${fmt(Math.round(f.tokens / 1000) || 1)}k token${online && !f.online_ok ? " · 不允许发送到在线模型" : ""}</span></button>`).join("");
    if (keepInput && box.querySelector("#pick-q")) {
      box.querySelector(".pick-list").innerHTML = list || '<div class="hint">没有找到，换个文件名试试</div>';
      return;
    }
    box.innerHTML = `${p.fromAt ? "" : '<input id="pick-q" class="mini" placeholder="按文件名查找已索引的文件" autocomplete="off">'}
      <div class="pick-list" role="listbox" aria-label="附加文件">${list || '<div class="hint">输入文件名查找</div>'}</div>
      <div class="hint pick-foot">${online ? "在线模型会读附加文件的全文" : "本机模型会只在附加的文件里检索"} · ↑↓ 选择 · ↩ 附加 · Esc 关闭</div>`;
    box.hidden = false;
    const inp = box.querySelector("#pick-q");
    if (inp) { inp.value = p.query; inp.focus(); inp.addEventListener("input", () => { p.query = inp.value; loadPicker(); }); inp.addEventListener("keydown", pickerKey); }
  }
  function pickerKey(e) {
    const p = S.picker;
    if (!p) return false;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      p.active = Math.max(0, Math.min(p.items.length - 1, p.active + (e.key === "ArrowDown" ? 1 : -1)));
      renderPicker(true); return true;
    }
    if (e.key === "Enter" && !imeActive(e)) { e.preventDefault(); if (p.items[p.active]) attachFile(p.items[p.active]); return true; }
    if (e.key === "Escape") { e.preventDefault(); closePicker(); $("q").focus(); return true; }
    return false;
  }
  function attachFile(f) {
    if (S.attach.some((x) => x.path === f.path)) { toast("已经附加过了"); }
    else if (S.attach.length >= 10) { toast("一次最多附加 10 个文件"); }
    else S.attach.push(f);
    if (S.picker && S.picker.fromAt) {   // 去掉输入框里的 @文件名
      const t = $("q"), pos = t.selectionStart, before = t.value.slice(0, pos).replace(/@[^\s@]*$/, "");
      t.value = before + t.value.slice(pos);
      t.selectionStart = t.selectionEnd = before.length;
    }
    closePicker(); renderComposerTop(); $("q").focus();
  }
  async function attachPath(path) {
    const items = await api("find_files", baseName(path), 50);
    const f = items.find((x) => x.path === path);
    if (!f) { toast("这个文件还没有索引"); return; }
    if (S.mode === "search") setMode("qa");
    attachFile(f);
    toast(`已附加《${f.name}》`);
  }

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast("已复制"); }
    catch (e) {
      const t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select();
      document.execCommand("copy"); t.remove(); toast("已复制");
    }
  }

  // ---------------- 模型配置页 ----------------
  async function renderModels(data) {
    const box = $("models");
    if (!data) {
      box.innerHTML = '<div class="lib-head"><h1>模型</h1></div><div class="hint">正在读取…</div>';
      try { data = await api("models"); } catch (e) { box.innerHTML = `<div class="banner">${esc(e.message || e)}</div>`; return; }
    }
    S.mcfg = data;
    S.choices = data.choices; renderModelSelect(); renderComposerTop();
    const cfg = data.config, local = data.local;
    const ed = S.modelEdit;
    let h = `<div class="lib-head"><h1>模型</h1><span class="hint">检索和向量计算总在本机；回答可以用本机或在线模型，对话里每一轮都能换</span></div>`;

    // 在线模型
    h += `<section class="mcard"><div class="mhead"><h2>在线模型</h2><span class="grow"></span>
      <label class="check"><input type="checkbox" id="offline-mode"${cfg.offline_mode ? " checked" : ""}> 离线模式（禁止所有在线模型）</label>
      <button type="button" class="btn small primary" data-model-add="1"${cfg.offline_mode ? " disabled" : ""}>添加在线模型</button></div>`;
    if (!cfg.online.length && !(ed && !ed.id)) {
      h += `<div class="hint">还没有配置。添加后，对话里可以选它回答；本机检索到的资料片段会发送给它（发送前可以确认）。</div>`;
    }
    for (const m of cfg.online) {
      if (ed && ed.id === m.id) { h += modelForm(ed, data); continue; }
      h += `<div class="mrow"><div class="mi"><div class="mn">${esc(m.name)}${m.enabled ? "" : '<span class="tagx">已停用</span>'}</div>
        <div class="md">${esc(m.model)} · ${m.format === "anthropic" ? "Anthropic 接口" : "OpenAI 兼容接口"} · ${esc(m.base_url)} · 上下文 ${fmt(m.context_window)}
        · 密钥 ${m.key_set ? "…" + esc(m.key_tail || "已填") : "未填"}${m.web ? ` · <span class="hint">${esc(m.web)}</span>` : ""}</div></div>
        <button type="button" class="btn small" data-model-test="${esc(m.id)}">测试</button>
        <button type="button" class="btn small" data-model-edit="${esc(m.id)}">编辑</button>
        <button type="button" class="btn small danger" data-model-del="${esc(m.id)}">删除</button></div>`;
    }
    if (ed && !ed.id) h += modelForm(ed, data);
    h += `<div class="setting inline"><div class="txt"><div class="h" id="cf-label">发送前逐段确认</div>
      <div class="d">用在线模型时，先列出要发送的资料片段，你确认（可以去掉一些）后才发送。</div></div>
      <button type="button" class="switch" role="switch" aria-checked="${S.settings.confirm_online !== false}" aria-labelledby="cf-label" id="cf-switch"></button></div>`;
    h += `</section>`;

    // 本机模型
    const opts = (local.models || []).filter((n) => !/embed|bge|e5|minilm/i.test(n));
    const cur = cfg.local.chat;
    h += `<section class="mcard"><div class="mhead"><h2>本机模型</h2><span class="hint">${local.ok ? `Ollama 里有 ${local.models.length} 个模型` : esc(local.message || "连不上 Ollama")}</span></div>
      <form class="mform" id="local-form">
        <label>回答用的模型<select name="chat" class="mini">${[...new Set([cur, ...opts])].map((n) => `<option${n === cur ? " selected" : ""}>${esc(n)}</option>`).join("")}</select></label>
        <label>上下文长度<input name="ctx" class="mini" type="number" min="2048" step="1024" value="${cfg.local.context_window}"></label>
        <button type="submit" class="btn small primary">保存</button>
        <div class="hint wide">8 GB 内存建议 qwen3:4b-instruct、上下文 4096；换了内存更大的电脑，可以先在终端运行 <code>ollama pull qwen3:14b</code> 之类的命令下载，再在这里选择。上下文越长，能带的资料和对话越多，也越占内存。</div>
      </form></section>`;

    // 向量模型
    const embOpts = (local.models || []).filter((n) => /embed|bge|e5|minilm|gte/i.test(n));
    h += `<section class="mcard"><div class="mhead"><h2>向量模型（Embedding）</h2><span class="hint">现在用：${esc(data.embedding_in_use.replace(/^local\//, ""))} · ${data.embedding_dim} 维</span></div>
      <form class="mform" id="emb-form">
        <label>更换为<select name="emb" class="mini">${[...new Set([cfg.embedding.model, ...embOpts])].map((n) => `<option${n === cfg.embedding.model ? " selected" : ""}>${esc(n)}</option>`).join("")}</select></label>
        <button type="submit" class="btn small">更换…</button>
        <div class="hint wide">向量模型决定“按意思找资料”的效果。更换后旧索引不能再用，需要重新索引（100 GB 资料要一到几天）；问答记录会保留。下拉框里只列出 Ollama 里名字像向量模型的（如 bge-m3、nomic-embed-text）。</div>
      </form><div id="emb-flow"></div></section>`;
    box.innerHTML = h;
    bindModelsPage();
  }

  function modelForm(ed, data) {
    const presets = data.presets, g = data.guide;
    const p = presets.find((x) => x.id === ed.preset);
    const field = (key, input) => `<label class="ff"><span class="fl">${esc(g[key].label)}</span>${input}
      <span class="fh">${esc(g[key].hint)}${g[key].example ? `<span class="ex">例：${esc(g[key].example)}</span>` : ""}</span></label>`;
    return `<form class="mform edit" id="model-form">
      <label class="ff"><span class="fl">服务</span><select name="preset" class="mini">
        ${Object.entries(data.groups || { vendor: "" }).map(([gid, glabel]) => `<optgroup label="${esc(glabel)}">${presets.filter((x) => (x.group || "vendor") === gid).map((x) => `<option value="${x.id}"${x.id === ed.preset ? " selected" : ""}>${esc(x.name)}</option>`).join("")}</optgroup>`).join("")}
        <option value="custom"${ed.preset === "custom" ? " selected" : ""}>自定义接口…</option></select>
        <span class="fh">${p ? `去 <span class="ex">${esc(p.web)}</span> 创建密钥` : "填写任意兼容 OpenAI 或 Anthropic 格式的接口"}</span></label>
      ${field("name", `<input name="name" class="mini" value="${esc(ed.name || "")}" placeholder="${esc(g.name.example)}">`)}
      ${field("base_url", `<input name="base_url" class="mini" value="${esc(ed.base_url || "")}" placeholder="${esc(g.base_url.example)}">`)}
      ${ed.preset === "custom" ? field("web", `<input name="web" class="mini" value="${esc(ed.web || "")}" placeholder="${esc(g.web.example)}">`) : ""}
      ${field("format", `<select name="format" class="mini"${ed.preset !== "custom" ? " disabled" : ""}><option value="openai"${ed.format !== "anthropic" ? " selected" : ""}>OpenAI 兼容</option><option value="anthropic"${ed.format === "anthropic" ? " selected" : ""}>Anthropic</option></select>`)}
      ${field("api_key", `<input name="api_key" class="mini" type="password" autocomplete="off" value="${esc(ed.api_key || "")}" placeholder="${ed.id && ed.key_set ? "已保存（不改就留空）" : esc(p ? p.key_hint : g.api_key.example)}">`)}
      ${field("model", `<span class="row-in"><input name="model" class="mini" value="${esc(ed.model || "")}" placeholder="${esc(p && p.models[0] ? p.models[0] : g.model.example)}">
        <select name="model_pick" class="mini model-pick" aria-label="从列表选择模型"${(p && p.models.length) ? "" : " hidden"}>${modelPickOptions(p ? p.models : [], ed.model)}</select>
        <button type="button" class="btn small" data-model-list="1">获取模型列表</button></span>`)}
      ${field("context_window", `<input name="context_window" class="mini" type="number" min="2048" value="${ed.context_window || (p ? p.context_window : 32000)}">`)}
      <label class="check"><input type="checkbox" name="enabled"${ed.enabled !== false ? " checked" : ""}> 启用（在对话的模型下拉框里出现）</label>
      <div class="mfoot"><span class="hint" id="model-test-msg">密钥明文保存在本机 data/models.json（不进 Git）</span><span class="grow"></span>
        <button type="button" class="btn small" data-model-test-form="1">测试连接</button>
        <button type="button" class="btn small" data-model-cancel="1">取消</button>
        <button type="submit" class="btn small primary">保存</button></div></form>`;
  }

  function modelPickOptions(list, current) {
    return `<option value="">从列表选择…</option>` + list.map((m) => `<option value="${esc(m)}"${m === current ? " selected" : ""}>${esc(m)}</option>`).join("");
  }

  function formEntry() {
    const f = $("model-form");
    if (!f) return null;
    const ed = S.modelEdit, p = S.mcfg.presets.find((x) => x.id === f.preset.value);
    return {
      id: ed.id || null, preset: f.preset.value, name: f.name.value.trim() || (p ? p.name : "自定义模型"),
      base_url: f.base_url.value.trim(), web: f.web ? f.web.value.trim() : (p ? p.web : ""),
      format: p ? p.format : f.format.value, api_key: f.api_key.value.trim(), model: f.model.value.trim(),
      context_window: Number(f.context_window.value) || 32000, enabled: f.enabled.checked,
    };
  }

  function bindModelsPage() {
    const mf = $("model-form");
    if (mf) {
      mf.model_pick.addEventListener("change", () => { if (mf.model_pick.value) mf.model.value = mf.model_pick.value; });
      mf.preset.addEventListener("change", () => {
        const p = S.mcfg.presets.find((x) => x.id === mf.preset.value);
        S.modelEdit = Object.assign(formEntry(), p ? { preset: p.id, name: p.name, base_url: p.base_url, format: p.format,
          web: p.web, context_window: p.context_window, model: p.models[0] || "", api_key: "" }
          : { preset: "custom", base_url: "", name: "", web: "", format: "openai", model: "", context_window: 32000, api_key: "" });
        S.modelList = null;
        renderModels(S.mcfg);
      });
      mf.addEventListener("submit", async (e) => {
        e.preventDefault();
        const r = await api("save_online", formEntry());
        if (r.error) { $("model-test-msg").textContent = r.error; return; }
        S.modelEdit = null; S.modelList = null; toast("已保存，对话里可以选这个模型了");
        renderModels(r);
      });
    }
    $("offline-mode").addEventListener("change", async (e) => {
      const r = await api("set_offline", e.target.checked);
      toast(e.target.checked ? "离线模式：只用本机模型" : "已允许使用在线模型");
      renderModels(r);
    });
    $("cf-switch").addEventListener("click", async () => {
      S.settings = await api("set_setting", "confirm_online", S.settings.confirm_online === false);
      renderModels(S.mcfg);
    });
    $("local-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const r = await api("set_local", f.chat.value, Number(f.ctx.value) || null, null);
      toast("已保存本机模型设置");
      renderModels(r);
    });
    $("emb-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const model = e.target.emb.value;
      $("emb-flow").innerHTML = '<div class="hint">正在用这个模型试算一次向量…</div>';
      const r = await api("probe_embedding", model);
      if (r.error) { $("emb-flow").innerHTML = `<div class="banner">${esc(r.error)}</div>`; return; }
      if (r.current === "local/" + r.model) { $("emb-flow").innerHTML = '<div class="hint">这就是现在用的向量模型，不需要更换。</div>'; return; }
      $("emb-flow").innerHTML = `<form class="embflow" id="emb-apply"><h3>更换为 ${esc(r.model)}（${r.dim} 维）</h3>
        <p>旧的索引会清空（问答记录保留）。选择要重新索引的文件夹，没勾选的会从资料库移除：</p>
        <div class="opts">${r.sources.map((s) => `<label class="check"><input type="checkbox" name="src" value="${s.id}" checked> ${esc(s.path)} <span class="hint">${fmt(s.files)} 个文件</span></label>`).join("") || '<span class="hint">资料库里还没有文件夹</span>'}</div>
        <p>什么时候重新索引：</p>
        <label class="check"><input type="radio" name="when" value="idle" checked> 闲时自动（接通电源、电脑空闲时进行，问答时让路）</label>
        <label class="check"><input type="radio" name="when" value="now"> 现在开始</label>
        <div class="mfoot"><span class="grow"></span><button type="button" class="btn small" data-emb-cancel="1">取消</button>
          <button type="submit" class="btn small danger">清空旧索引并更换</button></div></form>`;
      $("emb-apply").addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const f = ev.target, ids = [...f.querySelectorAll('input[name="src"]:checked')].map((x) => Number(x.value));
        const when = f.querySelector('input[name="when"]:checked').value;
        $("emb-flow").innerHTML = '<div class="hint">正在更换…</div>';
        const res = await api("apply_embedding", r.model, r.dim, ids, when);
        if (res.error) { $("emb-flow").innerHTML = `<div class="banner">${esc(res.error)}</div>`; return; }
        toast(`已更换向量模型，${res.folders} 个文件夹${when === "now" ? "开始" : "将在闲时"}重新索引`);
        refreshLibrary(); renderModels(res);
      });
    });
  }

  // ---------------- 资料库 ----------------
  function showView(v) {
    if (v === "logs" && !S.settings.dev_mode) v = "settings";
    S.view = v;
    $("view-qa").hidden = v !== "qa"; $("aside").hidden = v !== "qa";
    $("view-lib").hidden = v !== "lib"; $("lib-aside").hidden = v !== "lib";
    $("view-settings").hidden = v !== "settings"; $("view-logs").hidden = v !== "logs";
    $("view-models").hidden = v !== "models";
    $("btn-settings").classList.toggle("active", v === "settings");
    $("btn-models").classList.toggle("active", v === "models");
    if (v === "models") { S.modelEdit = null; renderModels(); }
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
      <div class="drop" id="drop">把文件夹拖到这里添加 · 支持 <span id="drop-formats">${esc(FORMATS || "PDF、Word、PowerPoint、Markdown")}</span></div>`;
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
          ${s.allow_online === false ? "<span>不发送到在线模型</span>" : ""}
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
      <div class="fset-row top"><span class="k">在线模型</span>
        <div class="grow"><label class="check"><input type="checkbox" name="allow_online"${s.allow_online !== false ? " checked" : ""}> 允许把这个文件夹的内容发送到在线模型</label>
        <div class="hint">关掉后，这里的资料只给本机模型用：用在线模型时会自动排除，也不能附加这里的文件。</div></div></div>
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
      allow_online: form.allow_online.checked,
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
    if (d.ref) return select(d.ref, d.turn);
    if (d.kfile) return select(d.kfile, d.turn);
    if (d.sel) return select(d.sel);
    if (d.thread) return openThread(d.thread);
    if (d.lib) return showView("lib");
    if (d.open != null) { const r = await api("open_file", d.open); if (r.error) toast(r.error); return; }
    if (d.reveal != null) { const r = await api("reveal", d.reveal); if (r.error) toast(r.error); return; }
    if (d.branch) return branchFrom(d.branch);
    if (d.copyTurn) {
      const r = S.thread && S.thread.turns.find((x) => String(x.id) === d.copyTurn);
      if (r) copy(r.answer.replace(/\s*\[\d+(?:\s*[,，、]\s*\d+)*\]/g, ""));
      return;
    }
    if (d.askAnyway) {
      const r = S.thread.turns.find((x) => String(x.id) === d.askAnyway);
      if (r) { setMode("qa"); submit(r.question, { force: true, mode: "qa" }); }
      return;
    }
    if (d.searchInstead) {
      const r = S.thread.turns.find((x) => String(x.id) === d.searchInstead);
      setMode("search"); $("q").value = r ? r.question : ""; $("q").focus(); return;
    }
    if (d.attach != null) return attachPath(d.attach);
    if (d.unattach != null) { S.attach.splice(Number(d.unattach), 1); renderComposerTop(); return; }
    if (d.pick != null) return attachFile(S.picker.items[Number(d.pick)]);
    if (d.cfAll) { const cf = S.cur.confirm; cf.keep = cf.keep.size === cf.items.length ? new Set() : new Set(cf.items.map((x) => x.n)); return renderConfirm(); }
    if (d.cfCancel) { const qid = S.cur.qid; S.cur.confirm = null; renderConfirm(); await api("confirm_send", qid, null); return; }
    if (d.cfSend) { const qid = S.cur.qid, keep = [...S.cur.confirm.keep]; S.cur.confirm = null; renderConfirm(); renderQA(); await api("confirm_send", qid, keep); return; }
    if (d.consentNo) { $("consent").hidden = true; S.consentOk = null; $("q").focus(); return; }
    if (d.consentYes) { $("consent").hidden = true; const f = S.consentOk; S.consentOk = null; if (f) await f(); return; }
    if (d.modelAdd) { const p = S.mcfg.presets[0]; S.modelEdit = { preset: p.id, name: p.name, base_url: p.base_url, format: p.format, web: p.web, context_window: p.context_window, model: p.models[0] || "" }; S.modelList = null; return renderModels(S.mcfg); }
    if (d.modelEdit) { S.modelEdit = Object.assign({}, S.mcfg.config.online.find((m) => m.id === d.modelEdit)); S.modelList = null; return renderModels(S.mcfg); }
    if (d.modelCancel) { S.modelEdit = null; return renderModels(S.mcfg); }
    if (d.modelDel) { const r = await api("delete_online", d.modelDel); toast("已删除"); return renderModels(r); }
    if (d.modelTest) { toast("正在测试…"); const r = await api("test_online", { id: d.modelTest }); toast(r.ok ? "连接正常：" + r.message : r.message); return; }
    if (d.modelTestForm) { $("model-test-msg").textContent = "正在测试…"; const r = await api("test_online", formEntry()); $("model-test-msg").textContent = (r.ok ? "✓ " : "✗ ") + r.message; return; }
    if (d.modelList) {
      // 只更新下拉框，不重画表单：已经填的密钥、名称等都保留
      const f = $("model-form"), msg = $("model-test-msg");
      msg.textContent = "正在获取模型列表…";
      const r = await api("list_online_models", formEntry());
      if (r.error) { msg.textContent = r.error; return; }
      const pick = f.model_pick;
      pick.innerHTML = modelPickOptions(r.models, f.model.value.trim());
      pick.hidden = !r.models.length;
      msg.textContent = r.models.length ? `获取到 ${r.models.length} 个模型，在右边的下拉框里选择` : "服务没有返回模型列表，请手动填写模型名";
      if (r.models.length) pick.focus();
      return;
    }
    if (d.embCancel) { $("emb-flow").innerHTML = ""; return; }
    if (d.scopeFile != null) { setScopeFile(d.scopeFile); if (S.mode === "search") setMode("qa"); toast(`接下来只在《${baseName(d.scopeFile)}》里找`); $("q").focus(); return; }
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
      case "new": return newQuestion();
      case "update-all": { const r = await api("update_all"); toast(r.queued ? `${r.queued} 个文件夹已加入更新队列` : "已经在更新了"); return; }
      case "pause": await api("pause_index"); return refreshLibrary();
      case "resume": await api("resume_index"); return refreshLibrary();
      case "stop-index": await api("stop_index"); toast("已停止，下次空闲时或点“立即更新”继续"); return;
      case "remove-no": S.confirmRemove = null; return renderLib();
      case "fset-cancel": S.libEdit = null; return renderLib();
    }
  });
  $("confirm-send").addEventListener("change", (e) => {
    const n = Number(e.target.dataset.cf);
    if (!n || !S.cur || !S.cur.confirm) return;
    if (e.target.checked) S.cur.confirm.keep.add(n); else S.cur.confirm.keep.delete(n);
    const btn = $("confirm-send").querySelector("[data-cf-send]");
    if (btn) btn.textContent = `发送 ${S.cur.confirm.keep.size} 段`;
  });
  $("model-select").addEventListener("change", (e) => {
    if (e.target.value === "__config") { e.target.value = S.modelRef; return showView("models"); }
    S.modelRef = e.target.value;
    try { localStorage.setItem("lkos.model", S.modelRef); } catch (err) { /* 忽略 */ }
    renderModelSelect(); renderComposerTop();
  });
  $("attach-btn").addEventListener("click", () => (S.picker ? closePicker() : openPicker("", false)));
  $("btn-models").addEventListener("click", () => showView("models"));
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
    if (S.picker && !e.target.closest("#picker") && !e.target.closest("#attach-btn") && e.target.id !== "q") closePicker();
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
  $("q").addEventListener("input", () => {
    const t = $("q"), m = t.value.slice(0, t.selectionStart).match(/(?:^|\s)@([^\s@]*)$/);
    if (m && S.mode !== "search" && S.mode !== "chat") {
      if (!S.picker || !S.picker.fromAt) openPicker(m[1], true);
      else { S.picker.query = m[1]; loadPicker(); }
    } else if (S.picker && S.picker.fromAt) closePicker();
  });
  $("q").addEventListener("keydown", (e) => {
    if (S.picker && S.picker.fromAt && pickerKey(e)) return;
    if (e.key !== "Enter" || imeActive(e)) return;
    if (e.metaKey || e.ctrlKey) { e.preventDefault(); submit(); }
    // 普通回车：保持默认行为（换行）
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("consent").hidden) { $("consent").hidden = true; S.consentOk = null; return; }
    if (e.key === "Escape" && S.picker && !imeActive(e)) { closePicker(); $("q").focus(); return; }
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
