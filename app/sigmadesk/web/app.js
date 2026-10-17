/* SigmaCode Desk UI — vanilla JS, no build step. All text goes through textContent (no HTML injection). */
"use strict";

// ------------------------------------------------------------------ token & API
const TOKEN = (() => {
  const m = location.hash.match(/t=([\w-]+)/);
  if (m) { try { sessionStorage.setItem("deskToken", m[1]); } catch (e) {} history.replaceState(null, "", "/"); return m[1]; }
  try { return sessionStorage.getItem("deskToken") || ""; } catch (e) { return ""; }
})();

async function api(method, path, body, opts = {}) {
  const headers = { "X-Desk-Token": TOKEN };
  let payload = body;
  if (body && !(body instanceof FormData)) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  const res = await fetch("/api" + path, { method, headers, body: payload });
  if (opts.raw) return res;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}
const fileUrl = (path) => "/api" + path + (path.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(TOKEN);

// ------------------------------------------------------------------ i18n
const STR = {
  ar: {
    "nav.chat": "المحادثة", "nav.library": "المكتبة", "nav.search": "البحث", "nav.settings": "الإعدادات",
  },
  en: {
    "nav.chat": "Chat", "nav.library": "Library", "nav.search": "Search", "nav.settings": "Settings",
    "المكتبة": "Library", "المستندات": "Documents", "المجموعات": "Collections", "مجموعة جديدة": "New collection",
    "أضف ملفات": "Add files", "اسحب الملفات إلى هنا أو": "Drag files here or",
    "PDF ممسوح أو رقمي، صور، Word، نصوص": "Scanned or digital PDF, images, Word, text",
    "الاسم": "Name", "الحالة": "Status", "الصفحات": "Pages", "المقاطع": "Chunks", "الثقة": "Confidence",
    "بانتظار المعالجة": "Queued", "قيد المعالجة": "Processing", "بانتظار مراجعتك": "Needs review", "جاهز": "Ready", "خطأ": "Error",
    "لا توجد مستندات بعد.": "No documents yet.",
    "مراجعة النص": "Review text", "اعتماد وفهرسة": "Approve and index", "إعادة المعالجة": "Reprocess",
    "إعادة التعرّف الضوئي": "Re-run OCR", "حذف": "Delete", "رجوع": "Back", "حفظ": "Save", "الإصدار": "Version",
    "الصفحة": "Page", "نص الصفحة": "Page text", "صورة الصفحة": "Page image",
    "محادثة جديدة": "New conversation", "اكتب سؤالك": "Type your question", "إرسال": "Send",
    "ابحث في المصادر": "Search sources", "النص المعتمد": "Approved text", "الصفحة الأصلية": "Original page",
    "الإعدادات": "Settings", "الذكاء الاصطناعي": "AI", "النماذج المحلية": "Local models", "التعرّف الضوئي": "OCR",
    "الإجابة": "Answers", "البيانات": "Data", "اختبار الاتصال": "Test connection", "تنزيل": "Download",
    "استيراد ملف نموذج": "Import model file", "مثبت": "Installed", "اختر": "Use", "مستخدم": "In use",
  },
};
let LANG = "ar";
const t = (s) => (LANG === "ar" ? (STR.ar[s] || s) : (STR.en[s] || s));

// ------------------------------------------------------------------ DOM helpers
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const $view = () => document.getElementById("view");
function toast(msg, err = false) {
  const el = document.getElementById("toast");
  el.textContent = msg; el.className = "toast show" + (err ? " err" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => (el.className = "toast"), err ? 6000 : 2600);
}
async function guard(fn) { try { return await fn(); } catch (e) { toast(e.message || String(e), true); } }
const fmtPct = (x) => (x === null || x === undefined ? "—" : Math.round(x * 100) + "%");

// ------------------------------------------------------------------ state & routing
const S = { boot: null, view: "chat", collection: null, doc: null, chat: null, chats: [], src: null };

async function boot() {
  S.boot = await api("GET", "/bootstrap");
  LANG = S.boot.settings.ui_language || "ar";
  document.documentElement.lang = LANG;
  document.documentElement.dir = LANG === "ar" ? "rtl" : "ltr";
  document.querySelectorAll("[data-i18n]").forEach((el) => (el.textContent = t(el.dataset.i18n)));
  document.querySelectorAll(".nav").forEach((b) => b.addEventListener("click", () => go(b.dataset.view)));
  S.collection = S.boot.collections[0]?.id;
  setInterval(() => api("POST", "/ping").catch(() => {}), 5000);
  setInterval(refreshStatus, 4000);
  refreshStatus();
  const ready = S.boot.stats.ready;
  go(ready ? "chat" : "library");
}

function go(view, arg) {
  S.view = view;
  document.querySelectorAll(".nav").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  ({ chat: viewChat, library: viewLibrary, search: viewSearch, settings: viewSettings, doc: viewDoc }[view])(arg);
}

async function refreshStatus() {
  const st = await api("GET", "/ai/status").catch(() => null);
  if (!st) return;
  const s = S.boot.settings;
  const el = document.getElementById("status-line");
  const local = s.answer.provider === "local";
  const model = local ? (s.providers.local.model_file || "").split(/[\\/]/).pop() : (s.providers[s.answer.provider] || {}).model;
  el.replaceChildren(
    h("div", {}, h("span", { class: "dot" + (local ? (st.runtime.chat.running ? " on" : "") : " on") }),
      local ? (LANG === "ar" ? "محلي: " : "Local: ") : (s.answer.provider + ": "), model || (LANG === "ar" ? "لم يُحدد نموذج" : "no model")),
    st.embeddings ? h("div", {}, (LANG === "ar" ? "الفهرسة الدلالية: " : "Semantic index: ") + st.embeddings.done + "/" + st.embeddings.total) : null
  );
  if (S.view === "library" && document.querySelector("[data-live]")) loadDocs(false);
}

// ------------------------------------------------------------------ library
const STATUS = { queued: ["بانتظار المعالجة", ""], processing: ["قيد المعالجة", ""], review: ["بانتظار مراجعتك", "warn"], ready: ["جاهز", "ok"], error: ["خطأ", "err"] };

async function viewLibrary() {
  const cols = await api("GET", "/collections");
  if (!cols.find((c) => c.id === S.collection)) S.collection = cols[0]?.id;
  const list = h("div", {}, cols.map((c) =>
    h("button", { class: "coll" + (c.id === S.collection ? " active" : ""), onclick: () => { S.collection = c.id; viewLibrary(); } },
      h("span", {}, c.name), h("span", { class: "chip" }, c.documents))));
  const addCol = h("button", { class: "btn small", onclick: async () => {
    const name = prompt(t("مجموعة جديدة"));
    if (name) { await guard(() => api("POST", "/collections", { name })); viewLibrary(); }
  } }, "+ " + t("مجموعة جديدة"));

  const input = h("input", { type: "file", multiple: true, hidden: true, accept: ".pdf,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp,.docx,.txt,.md,.csv",
    onchange: (e) => upload(e.target.files) });
  const drop = h("div", { class: "drop" },
    h("div", {}, t("اسحب الملفات إلى هنا أو"), " ", h("button", { class: "btn primary", onclick: () => input.click() }, t("أضف ملفات"))),
    h("p", { class: "hint" }, t("PDF ممسوح أو رقمي، صور، Word، نصوص")), input);
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); upload(e.dataTransfer.files); });

  $view().replaceChildren(h("div", { class: "page" },
    h("h1", {}, t("المكتبة")),
    h("p", { class: "sub" }, LANG === "ar" ? "أضف مستنداتك: يُقرأ النص (وتُستخدم قراءة الصور للممسوح) ثم يُقطّع ويُفهرس على جهازك دون إنترنت." : "Add documents: text is read (OCR for scans), chunked and indexed on this computer, offline."),
    h("div", { class: "lib" },
      h("aside", { class: "card" }, h("h2", {}, t("المجموعات")), list, h("div", { style: "margin-top:10px" }, addCol)),
      h("section", {}, drop, h("div", { class: "card", id: "docs", "data-live": "1" })))));
  loadDocs(true);
}

async function upload(files) {
  if (!files || !files.length) return;
  const fd = new FormData();
  fd.append("collection_id", S.collection);
  for (const f of files) fd.append("files", f);
  toast(LANG === "ar" ? "جارٍ رفع الملفات…" : "Uploading…");
  const res = await guard(() => api("POST", "/documents", fd));
  if (res) {
    const errs = res.filter((r) => r.error);
    const dups = res.filter((r) => r.duplicate);
    if (errs.length) toast(errs.map((e) => e.file + ": " + e.error).join(" · "), true);
    else if (dups.length) toast(LANG === "ar" ? "بعض الملفات موجودة مسبقاً في هذه المجموعة." : "Some files were already in this collection.");
    else toast(LANG === "ar" ? "أُضيفت الملفات، وتجري معالجتها الآن." : "Files added and being processed.");
  }
  loadDocs(true);
}

async function loadDocs(full) {
  const box = document.getElementById("docs");
  if (!box) return;
  const docs = await api("GET", "/documents?collection=" + S.collection).catch(() => []);
  if (!docs.length) { box.replaceChildren(h("div", { class: "empty" }, t("لا توجد مستندات بعد."))); return; }
  box.replaceChildren(h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, t("الاسم")), h("th", {}, t("الحالة")), h("th", {}, t("الصفحات")), h("th", {}, t("المقاطع")), h("th", {}, t("الثقة")))),
    h("tbody", {}, docs.map((d) => {
      const [label, cls] = STATUS[d.status] || [d.status, ""];
      const status = d.status === "processing"
        ? h("div", { class: "row" }, h("span", { class: "chip" }, t(label)), h("div", { class: "bar grow" }, h("i", { style: `width:${Math.round((d.progress || 0) * 100)}%` })))
        : h("span", { class: "chip " + cls, title: d.error || "" }, t(label));
      return h("tr", { class: "click", onclick: () => go("doc", d.id) },
        h("td", {}, h("div", {}, d.name), h("div", { class: "hint ltr", style: "text-align:start" }, d.filename + (d.version ? " · v" + d.version : ""))),
        h("td", {}, status), h("td", {}, d.page_count || "—"), h("td", {}, d.chunks || "—"),
        h("td", {}, d.method === "text" ? h("span", { class: "chip ok" }, LANG === "ar" ? "نص رقمي" : "digital text") : fmtPct(d.confidence)));
    }))));
}

// ------------------------------------------------------------------ document review
async function viewDoc(id) {
  const doc = await guard(() => api("GET", "/documents/" + id));
  if (!doc) return go("library");
  const pages = await api("GET", `/documents/${id}/pages`).catch(() => []);
  let current = pages[0]?.page_no || 1;
  const hasImage = doc.kind === "pdf" || doc.kind === "image";
  const textArea = h("textarea", { class: "pagetext", dir: "auto", "aria-label": t("نص الصفحة") });
  const img = hasImage ? h("img", { class: "pageimg", alt: t("صورة الصفحة") }) : null;
  const pageBar = h("div", { class: "pagelist" });
  const saveBtn = h("button", { class: "btn", onclick: savePage }, t("حفظ"));
  let dirty = false;
  textArea.addEventListener("input", () => (dirty = true));

  function showPage(n) {
    current = n;
    const p = pages.find((x) => x.page_no === n);
    textArea.value = p ? p.text : "";
    dirty = false;
    if (img) img.src = fileUrl(`/documents/${id}/pages/${n}/image`);
    pageBar.querySelectorAll(".pagebtn").forEach((b) => b.classList.toggle("active", Number(b.dataset.n) === n));
  }
  async function savePage() {
    await guard(() => api("PUT", `/documents/${id}/pages/${current}`, { text: textArea.value }));
    const p = pages.find((x) => x.page_no === current); if (p) { p.text = textArea.value; p.edited = 1; }
    dirty = false; toast(LANG === "ar" ? "حُفظ نص الصفحة." : "Page saved.");
  }
  pages.forEach((p) => pageBar.append(h("button", { class: "pagebtn" + (p.method === "ocr" && (p.confidence ?? 1) < 0.85 ? " low" : ""),
    "data-n": p.page_no, title: p.method === "ocr" ? fmtPct(p.confidence) : "", onclick: async () => { if (dirty) await savePage(); showPage(p.page_no); } }, p.page_no)));

  const [label, cls] = STATUS[doc.status] || [doc.status, ""];
  const versionInput = h("input", { class: "input", value: doc.version || "", style: "width:120px", placeholder: t("الإصدار") });
  const nameInput = h("input", { class: "input", value: doc.name });
  $view().replaceChildren(h("div", { class: "page" },
    h("div", { class: "row", style: "margin-bottom:12px" },
      h("button", { class: "btn", onclick: () => go("library") }, (LANG === "ar" ? "→ " : "← ") + t("رجوع")),
      h("div", { class: "grow" }, nameInput), versionInput,
      h("button", { class: "btn", onclick: async () => { await guard(() => api("PATCH", "/documents/" + id, { name: nameInput.value, version: versionInput.value })); toast("✓"); } }, t("حفظ")),
      h("span", { class: "chip " + cls }, t(label))),
    doc.status === "review" ? h("div", { class: "banner warn" }, LANG === "ar"
      ? `قُرئ هذا المستند بالتعرّف الضوئي بثقة ${fmtPct(doc.confidence)}. راجع نص الصفحات (الصفحات ذات الإطار البرتقالي أضعف)، صحّح ما يلزم، ثم اضغط «اعتماد وفهرسة».`
      : `OCR confidence ${fmtPct(doc.confidence)}. Check the pages (orange = weaker), correct as needed, then press "Approve and index".`) : null,
    doc.status === "error" ? h("div", { class: "banner warn" }, doc.error) : null,
    h("div", { class: "row", style: "margin-bottom:14px" },
      doc.status !== "processing" && doc.status !== "queued" ? h("button", { class: "btn primary", onclick: async () => {
        if (dirty) await savePage();
        const d = await guard(() => api("POST", `/documents/${id}/approve`));
        if (d) { toast(LANG === "ar" ? "اعتُمد المستند وفُهرس." : "Approved and indexed."); viewDoc(id); }
      } }, doc.status === "ready" ? (LANG === "ar" ? "إعادة الفهرسة بعد التعديل" : "Re-index after edits") : t("اعتماد وفهرسة")) : null,
      h("button", { class: "btn", onclick: async () => { await guard(() => api("POST", `/documents/${id}/reprocess`, {})); go("library"); } }, t("إعادة المعالجة")),
      doc.kind === "pdf" ? h("button", { class: "btn", onclick: async () => { await guard(() => api("POST", `/documents/${id}/reprocess`, { force_ocr: true })); go("library"); } }, t("إعادة التعرّف الضوئي")) : null,
      h("a", { class: "btn", href: fileUrl(`/documents/${id}/file`), target: "_blank" }, LANG === "ar" ? "الملف الأصلي" : "Original file"),
      h("span", { class: "grow" }),
      h("button", { class: "btn danger", onclick: async () => {
        if (!confirm(LANG === "ar" ? "حذف المستند وكل ما فُهرس منه؟" : "Delete this document and its index?")) return;
        await guard(() => api("DELETE", "/documents/" + id)); go("library");
      } }, t("حذف"))),
    h("p", { class: "hint" }, `${doc.page_count || 0} ${t("الصفحات")} · ${doc.chunks.length} ${t("المقاطع")}`
      + (doc.chunks.some((c) => c.needs_review) ? (LANG === "ar" ? " · بعض المقاطع ضعيفة القراءة ولن تُستخدم في الإجابات حتى تُصحح الصفحة وتُعتمد" : " · some weak chunks are excluded until corrected") : "")),
    pages.length ? h("div", {}, pageBar, h("div", { class: hasImage ? "review" : "" },
      h("div", { class: "card" }, h("div", { class: "row", style: "margin-bottom:8px" }, h("h2", { class: "grow" }, t("نص الصفحة")), saveBtn), textArea),
      img ? h("div", { class: "card" }, h("h2", {}, t("صورة الصفحة")), img) : null)) : null));
  if (pages.length) showPage(current);
}

// ------------------------------------------------------------------ chat
function splitCites(text) {
  const out = []; const re = /\[(\d{1,3}(?:\s*[,،]\s*\d{1,3})*)\]/g; let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push({ text: text.slice(last, m.index) });
    m[1].split(/\s*[,،]\s*/).forEach((n) => out.push({ cite: Number(n) }));
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last) });
  return out;
}
function srcLabel(s) {
  let name = s.document_name + (s.document_version ? (LANG === "ar" ? "، إصدار " : ", edition ") + s.document_version : "");
  if (s.page_start) name += (LANG === "ar" ? "، صفحة " : ", page ") + (s.page_end && s.page_end !== s.page_start ? `${s.page_start}–${s.page_end}` : s.page_start);
  return name;
}

async function viewChat(chatId) {
  S.chats = await api("GET", "/chats");
  const cols = await api("GET", "/collections");
  if (chatId === undefined) chatId = S.chat?.id || S.chats[0]?.id;
  S.chat = chatId ? await api("GET", "/chats/" + chatId).catch(() => null) : null;
  const s = S.boot.settings;
  const needModel = s.answer.provider === "local" && !s.providers.local.model_file;

  const list = h("div", { class: "convs-list" }, S.chats.map((c) =>
    h("button", { class: "conv" + (S.chat && S.chat.id === c.id ? " active" : ""), onclick: () => viewChat(c.id), title: c.title }, c.title)));
  const newBtn = h("button", { class: "btn primary", onclick: async () => {
    const c = await guard(() => api("POST", "/chats", { collection_ids: [] }));
    if (c) viewChat(c.id);
  } }, "+ " + t("محادثة جديدة"));

  const thread = h("div", { class: "thread" });
  const col = h("div", { class: "col" });
  thread.append(col);
  const ta = h("textarea", { class: "input", rows: 2, dir: "auto", placeholder: t("اكتب سؤالك"), disabled: !S.chat || needModel });
  const send = h("button", { class: "btn primary", disabled: !S.chat || needModel }, t("إرسال"));
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(); } });
  send.addEventListener("click", ask);

  const scope = S.chat ? h("div", { class: "multisel" }, h("span", { class: "hint" }, LANG === "ar" ? "ابحث في:" : "Search in:"),
    cols.map((c) => {
      const cb = h("input", { type: "checkbox", checked: S.chat.collection_ids.includes(c.id) || !S.chat.collection_ids.length,
        onchange: async () => {
          const ids = [...scope.querySelectorAll("input:checked")].map((x) => Number(x.value));
          S.chat.collection_ids = ids.length === cols.length ? [] : ids;
          await api("PATCH", "/chats/" + S.chat.id, { collection_ids: S.chat.collection_ids });
        }, value: c.id });
      return h("label", {}, cb, c.name);
    })) : null;

  const root = h("div", { class: "chat" + (S.src ? " with-src" : "") },
    h("aside", { class: "convs" }, h("div", { class: "convs-head" }, newBtn), list),
    h("section", { class: "thread-wrap" },
      h("div", { class: "thread-head" }, h("strong", { class: "grow" }, S.chat ? S.chat.title : t("nav.chat")), scope,
        S.chat ? h("button", { class: "btn small danger", onclick: async () => {
          if (!confirm(LANG === "ar" ? "حذف المحادثة؟" : "Delete conversation?")) return;
          await api("DELETE", "/chats/" + S.chat.id); S.chat = null; viewChat(null);
        } }, t("حذف")) : null),
      thread,
      h("div", { class: "composer" }, h("div", { class: "col" }, ta, send))),
    S.src ? sourcePanel() : null);
  $view().replaceChildren(root);

  if (needModel) col.append(h("div", { class: "banner warn" }, LANG === "ar"
    ? "لم يُحدد نموذج ذكاء اصطناعي بعد. افتح «الإعدادات ← النماذج المحلية» ونزّل نموذجاً أو استورده (مرة واحدة، ثم يعمل دون إنترنت)، أو اختر مزوّداً آخر."
    : "No AI model selected. Open Settings → Local models to download or import one (once, then offline), or choose a provider.",
    " ", h("button", { class: "btn small", onclick: () => go("settings", "models") }, t("الإعدادات"))));
  if (!S.chat) col.append(h("div", { class: "empty" }, LANG === "ar" ? "ابدأ محادثة جديدة واسأل عن محتوى مستنداتك. كل إجابة تحيل إلى المستند والصفحة." : "Start a conversation and ask about your documents. Every answer cites its document and page."));
  else S.chat.messages.forEach((m) => col.append(renderMsg(m)));
  thread.scrollTop = thread.scrollHeight;
  ta.focus();

  async function ask() {
    const q = ta.value.trim();
    if (!q || send.disabled) return;
    ta.value = ""; send.disabled = true;
    col.append(h("div", { class: "q", dir: "auto" }, q));
    const draft = h("div", { class: "a draft", dir: "auto" });
    const wait = h("div", { class: "wait" }, h("span", { class: "dot-anim" }), LANG === "ar" ? "يبحث في مصادرك ويكتب الإجابة…" : "Searching your sources…");
    col.append(wait);
    thread.scrollTop = thread.scrollHeight;
    try {
      const final = await streamAsk(S.chat.id, q, (txt) => {
        if (!draft.isConnected) { wait.replaceWith(draft); }
        draft.textContent += txt; thread.scrollTop = thread.scrollHeight;
      });
      wait.remove(); draft.remove();
      col.append(renderMsg(final));
    } catch (e) {
      wait.remove(); draft.remove(); ta.value = q; toast(e.message, true);
    } finally {
      send.disabled = false; thread.scrollTop = thread.scrollHeight; ta.focus();
      S.chats = await api("GET", "/chats"); list.replaceChildren(...S.chats.map((c) =>
        h("button", { class: "conv" + (c.id === S.chat.id ? " active" : ""), onclick: () => viewChat(c.id) }, c.title)));
    }
  }
}

async function streamAsk(chatId, question, onDelta) {
  const res = await api("POST", `/chats/${chatId}/stream`, { question }, { raw: true });
  if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || res.statusText); }
  const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "", final = null;
  const handle = (line) => {
    if (!line.trim()) return;
    const ev = JSON.parse(line);
    if (ev.type === "delta") onDelta(ev.data); else if (ev.type === "final") final = ev.data;
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i; while ((i = buf.indexOf("\n")) >= 0) { handle(buf.slice(0, i)); buf = buf.slice(i + 1); }
  }
  handle(buf);
  if (!final) throw new Error(LANG === "ar" ? "انقطعت الإجابة." : "The answer was interrupted.");
  return final;
}

function renderMsg(m) {
  if (m.role === "user") return h("div", { class: "q", dir: "auto" }, m.content);
  const cls = m.is_error ? "error" : m.refused ? "refused" : m.grounded ? "grounded" : "";
  const body = h("div", { dir: "auto" }, splitCites(m.content).map((p) => p.text !== undefined ? p.text
    : h("button", { class: "cite", title: (m.sources.find((s) => s.n === p.cite) && srcLabel(m.sources.find((s) => s.n === p.cite))) || "",
      onclick: () => { const s = m.sources.find((x) => x.n === p.cite); if (s) openSource(s); } }, p.cite)));
  const rate = (v, sym) => h("button", { class: "rate" + (m.feedback === v ? " on" : ""), title: v === "up" ? (LANG === "ar" ? "مفيدة" : "Helpful") : (LANG === "ar" ? "غير مفيدة" : "Not helpful"),
    onclick: async (e) => { const nv = m.feedback === v ? "" : v; await api("POST", `/messages/${m.id}/feedback`, { value: nv }); m.feedback = nv;
      e.target.parentElement.querySelectorAll(".rate").forEach((b) => b.classList.remove("on")); if (nv) e.target.classList.add("on"); } }, sym);
  return h("div", { class: "a " + cls }, body,
    m.sources.length ? h("ul", { class: "sources" }, m.sources.map((s) => h("li", {}, h("button", { class: "src", onclick: () => openSource(s) }, h("span", { class: "cite" }, s.n), " ", srcLabel(s))))) : null,
    h("div", { class: "meta" }, m.is_error ? null : [rate("up", "👍"), rate("down", "👎")],
      m.model ? h("span", {}, m.model) : null, m.latency_ms ? h("span", {}, (m.latency_ms / 1000).toFixed(1) + "s") : null));
}

async function openSource(s) {
  const c = await guard(() => api("GET", "/chunks/" + s.chunk_id));
  if (!c) return;
  S.src = { ...c, label: srcLabel(s), tab: "text" };
  viewChat(S.chat?.id);
}

function sourcePanel() {
  const c = S.src;
  const body = h("div", { class: "srcpanel-body" });
  const tabs = h("div", { class: "tabs", style: "margin:0;padding:0 14px" });
  const render = () => {
    tabs.replaceChildren(
      h("button", { class: "tab" + (c.tab === "text" ? " active" : ""), onclick: () => { c.tab = "text"; render(); } }, t("النص المعتمد")),
      c.has_image ? h("button", { class: "tab" + (c.tab === "page" ? " active" : ""), onclick: () => { c.tab = "page"; render(); } }, t("الصفحة الأصلية")) : null);
    body.replaceChildren(c.tab === "text"
      ? h("div", { class: "srctext", dir: "auto" }, c.content)
      : h("img", { class: "pageimg", alt: t("الصفحة الأصلية"), src: fileUrl(`/documents/${c.document_id}/pages/${c.page_start || 1}/image`) }));
  };
  render();
  return h("aside", { class: "srcpanel", "aria-label": "Source" },
    h("div", { class: "srcpanel-head" }, h("strong", { class: "grow" }, c.label),
      h("button", { class: "btn small", onclick: () => go("doc", c.document_id) }, LANG === "ar" ? "فتح المستند" : "Open document"),
      h("button", { class: "btn small", "aria-label": "close", onclick: () => { S.src = null; viewChat(S.chat?.id); } }, "×")),
    tabs, body);
}

// ------------------------------------------------------------------ search
async function viewSearch() {
  const q = h("input", { class: "input", dir: "auto", placeholder: t("ابحث في المصادر") });
  const out = h("div", {});
  const run = async () => {
    const res = await guard(() => api("POST", "/search", { query: q.value, limit: 10 }));
    if (!res) return;
    out.replaceChildren(
      h("p", { class: "hint" }, (LANG === "ar" ? "طريقة البحث: " : "Mode: ") + ({ hybrid: LANG === "ar" ? "كلمات + معنى" : "keywords + meaning", lexical: LANG === "ar" ? "كلمات" : "keywords", lexical_fallback: LANG === "ar" ? "كلمات (تعذّر البحث بالمعنى)" : "keywords (semantic failed)" }[res.info.mode] || res.info.mode)),
      res.hits.length ? res.hits.map((hit, i) => h("div", { class: "card" },
        h("div", { class: "row" }, h("strong", { class: "grow" }, `${i + 1}. ${srcLabel(hit)}`),
          h("span", { class: "chip" }, hit.match), hit.similarity !== null ? h("span", { class: "chip ok" }, "≈ " + hit.similarity.toFixed(2)) : null),
        h("p", { dir: "auto" }, hit.snippet))) : h("div", { class: "empty" }, LANG === "ar" ? "لا نتائج." : "No results."));
  };
  q.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
  $view().replaceChildren(h("div", { class: "page" }, h("h1", {}, t("nav.search")),
    h("p", { class: "sub" }, LANG === "ar" ? "جرّب البحث كما يراه المساعد قبل أن يجيب." : "Try retrieval exactly as the assistant sees it."),
    h("div", { class: "row", style: "margin-bottom:16px" }, h("div", { class: "grow" }, q), h("button", { class: "btn primary", onclick: run }, t("nav.search"))), out));
  q.focus();
}

// ------------------------------------------------------------------ settings
const PROVIDERS = [
  ["local", "محلي على الجهاز", "Local (offline)", "دون إنترنت", "Offline"],
  ["ollama", "Ollama", "Ollama", "خادم محلي", "Local server"],
  ["openai", "OpenAI", "OpenAI", "يتطلب إنترنت ومفتاحاً", "Internet + key"],
  ["anthropic", "Claude", "Claude", "يتطلب إنترنت ومفتاحاً", "Internet + key"],
  ["google", "Gemini", "Gemini", "يتطلب إنترنت ومفتاحاً", "Internet + key"],
  ["custom", "مخصص (OpenAI-compatible)", "Custom (OpenAI-compatible)", "أي خادم متوافق", "Any compatible server"],
];

async function viewSettings(tab = "ai") {
  const s = await api("GET", "/settings");
  S.boot.settings = s;
  const tabs = [["ai", "الذكاء الاصطناعي"], ["models", "النماذج المحلية"], ["answer", "الإجابة"], ["ocr", "التعرّف الضوئي"], ["data", "البيانات"]];
  const body = h("div", {});
  $view().replaceChildren(h("div", { class: "page" }, h("h1", {}, t("الإعدادات")),
    h("div", { class: "tabs" }, tabs.map(([k, l]) => h("button", { class: "tab" + (k === tab ? " active" : ""), onclick: () => viewSettings(k) }, t(l)))), body));
  const save = async (patch, msg) => { const r = await guard(() => api("PUT", "/settings", patch)); if (r) { S.boot.settings = r; toast(msg || "✓"); refreshStatus(); } return r; };
  ({ ai: settingsAI, models: settingsModels, answer: settingsAnswer, ocr: settingsOCR, data: settingsData })[tab](body, s, save);
}

function settingsAI(body, s, save) {
  let prov = s.answer.provider;
  const grid = h("div", { class: "provgrid" });
  const fields = h("div", {});
  const draw = () => {
    grid.replaceChildren(...PROVIDERS.map(([k, ar, en, nar, nen]) => h("button", { class: "prov" + (k === prov ? " active" : ""), onclick: () => { prov = k; draw(); } },
      LANG === "ar" ? ar : en, h("small", {}, LANG === "ar" ? nar : nen))));
    const p = s.providers[prov] || {};
    if (prov === "local") {
      fields.replaceChildren(h("p", {}, LANG === "ar" ? "يعمل النموذج على هذا الجهاز ولا يُرسل أي نص خارجه. اختر النموذج من تبويب «النماذج المحلية»." : "Runs on this computer; no text leaves it. Pick the model in the Local models tab."),
        h("p", { class: "hint" }, (LANG === "ar" ? "النموذج الحالي: " : "Current model: ") + ((p.model_file || "").split(/[\\/]/).pop() || "—")));
      return;
    }
    const base = h("input", { class: "input ltr", value: p.base_url || "", placeholder: prov === "ollama" ? "http://127.0.0.1:11434/v1" : (LANG === "ar" ? "فارغ = الافتراضي" : "empty = default") });
    const key = h("input", { class: "input ltr", type: "password", value: p.api_key || "", placeholder: "sk-…" });
    const model = h("input", { class: "input ltr", value: p.model || "" });
    fields.replaceChildren(
      prov === "anthropic" ? h("div", { class: "banner" }, LANG === "ar" ? "Claude لا يوفّر بحثاً بالمعنى (تضمينات): اترك البحث بالمعنى على النموذج المحلي." : "Claude has no embeddings API: keep semantic search on the local model.") : null,
      h("label", { class: "field" }, h("span", {}, LANG === "ar" ? "عنوان الخادم" : "Base URL"), base),
      prov !== "ollama" ? h("label", { class: "field" }, h("span", {}, LANG === "ar" ? "مفتاح API (يُحفظ مشفّراً بحساب ويندوز)" : "API key (encrypted with your Windows account)"), key) : null,
      h("label", { class: "field" }, h("span", {}, LANG === "ar" ? "اسم النموذج" : "Model name"), model),
      h("div", {}, h("button", { class: "btn", onclick: async () => {
        await save({ providers: { [prov]: { base_url: base.value, model: model.value, ...(prov !== "ollama" ? { api_key: key.value } : {}) } } });
      } }, t("حفظ"))));
  };
  draw();
  const result = h("div", { class: "hint" });
  body.append(h("div", { class: "card" }, h("h2", {}, LANG === "ar" ? "مصدر الإجابات" : "Answer provider"), grid, fields,
    h("div", { class: "row", style: "margin-top:14px" },
      h("button", { class: "btn primary", onclick: async () => { await save({ answer: { provider: prov } }, LANG === "ar" ? "تم اختيار المزوّد." : "Provider selected."); } }, LANG === "ar" ? "اعتماد هذا المزوّد" : "Use this provider"),
      h("button", { class: "btn", onclick: async () => {
        result.textContent = LANG === "ar" ? "جارٍ الاختبار… (قد يستغرق تحميل النموذج المحلي دقيقة)" : "Testing… (loading a local model can take a minute)";
        const r = await guard(() => api("POST", "/settings/test"));
        if (!r) { result.textContent = ""; return; }
        result.textContent = (r.chat.ok ? "✓ " + (LANG === "ar" ? "الإجابة تعمل: " : "Chat OK: ") + r.chat.model : "✗ " + r.chat.error)
          + " · " + (r.embeddings.ok === null ? (LANG === "ar" ? "البحث بالمعنى غير مفعّل" : "semantic search off") : r.embeddings.ok ? "✓ " + (LANG === "ar" ? "البحث بالمعنى يعمل" : "embeddings OK") : "✗ " + r.embeddings.error);
      } }, t("اختبار الاتصال"))), result));

  const e = s.embeddings;
  const eprov = h("select", {}, [["local", LANG === "ar" ? "محلي (موصى به)" : "Local (recommended)"], ["none", LANG === "ar" ? "متوقف: كلمات فقط" : "Off: keywords only"],
    ["ollama", "Ollama"], ["openai", "OpenAI"], ["google", "Gemini"], ["custom", LANG === "ar" ? "مخصص" : "Custom"]].map(([v, l]) => h("option", { value: v, selected: v === e.provider }, l)));
  const emodel = h("input", { class: "input ltr", value: e.model || "", placeholder: "text-embedding-3-small · bge-m3 …" });
  const floor = h("input", { class: "input", type: "number", step: "0.05", min: "0", max: "0.95", value: e.min_similarity });
  body.append(h("div", { class: "card" }, h("h2", {}, LANG === "ar" ? "البحث بالمعنى (التضمينات)" : "Semantic search (embeddings)"),
    h("p", { class: "hint" }, LANG === "ar" ? "يلتقط الأسئلة المصاغة بكلمات مختلفة عن النص. النموذج المحلي يُختار من تبويب «النماذج المحلية»." : "Finds paraphrases. Pick the local model in the Local models tab."),
    h("label", { class: "field" }, h("span", {}, LANG === "ar" ? "المزوّد" : "Provider"), eprov),
    h("label", { class: "field" }, h("span", {}, LANG === "ar" ? "اسم نموذج التضمين (للمزوّدين الخارجيين)" : "Embedding model (remote providers)"), emodel),
    h("label", { class: "field" }, h("span", {}, LANG === "ar" ? "الحد الأدنى للتشابه" : "Minimum similarity"), floor),
    h("button", { class: "btn", onclick: () => save({ embeddings: { provider: eprov.value, model: emodel.value, min_similarity: Number(floor.value) } }) }, t("حفظ"))));
}

async function settingsModels(body, s, save) {
  const m = await api("GET", "/models");
  const installedBox = h("div", {});
  const catBox = h("div", {});
  const draw = (m) => {
    const used = [s.providers.local.model_file, s.embeddings.local_model_file];
    installedBox.replaceChildren(...(m.installed.length ? m.installed.map((f) => h("div", { class: "model" },
      h("div", {}, h("strong", { class: "ltr" }, f.name), h("div", { class: "hint" }, h("bdi", {}, `${f.size_gb} GB`), " · ", (f.role === "embed" ? (LANG === "ar" ? "نموذج بحث بالمعنى" : "embedding model") : (LANG === "ar" ? "نموذج إجابة" : "chat model")))),
      h("div", { class: "row" },
        used.includes(f.file) ? h("span", { class: "chip ok" }, t("مستخدم")) : h("button", { class: "btn small primary", onclick: async () => {
          const patch = f.role === "embed" ? { embeddings: { provider: "local", local_model_file: f.file } } : { providers: { local: { model_file: f.file } }, answer: { provider: "local" } };
          await save(patch, LANG === "ar" ? "اختير النموذج." : "Model selected."); viewSettings("models");
        } }, t("اختر")),
        h("button", { class: "btn small danger", onclick: async () => { if (confirm("?")) { await guard(() => api("DELETE", "/models/" + encodeURIComponent(f.name))); viewSettings("models"); } } }, t("حذف")))))
      : [h("p", { class: "hint" }, LANG === "ar" ? "لا توجد نماذج مثبتة بعد." : "No models installed yet.")]));
    catBox.replaceChildren(...m.catalog.map((c) => {
      const pr = m.progress[c.id];
      const pct = pr && pr.total ? Math.round((100 * pr.done) / pr.total) : 0;
      return h("div", { class: "model" },
        h("div", {}, h("strong", {}, c.name), " ", c.recommended ? h("span", { class: "chip ok" }, LANG === "ar" ? "موصى به" : "recommended") : null,
          h("div", { class: "hint" }, h("bdi", {}, `${c.size_gb} GB`), " · ", h("bdi", {}, `RAM ${c.ram_gb} GB`), " · ", h("bdi", {}, c.license), " · ", c.role === "embed" ? (LANG === "ar" ? "بحث بالمعنى" : "embeddings") : (LANG === "ar" ? "إجابة" : "chat")),
          h("div", { class: "hint" }, c.note),
          pr && pr.state === "downloading" ? h("div", { class: "bar", style: "margin-top:6px" }, h("i", { style: `width:${pct}%` })) : null,
          pr && pr.state === "error" ? h("div", { class: "hint", style: "color:var(--err)" }, pr.error) : null),
        h("div", {}, pr && pr.state === "downloading" ? h("span", { class: "chip" }, pct + "%")
          : h("button", { class: "btn small", onclick: async () => { await guard(() => api("POST", "/models/download", { id: c.id })); poll(); } }, t("تنزيل"))));
    }));
  };
  draw(m);
  let timer = null;
  const poll = () => { clearInterval(timer); timer = setInterval(async () => {
    if (!catBox.isConnected) return clearInterval(timer);
    const mm = await api("GET", "/models"); draw(mm);
    if (!Object.values(mm.progress).some((p) => p.state === "downloading")) clearInterval(timer);
  }, 1500); };
  if (Object.values(m.progress).some((p) => p.state === "downloading")) poll();
  const imp = h("input", { type: "file", accept: ".gguf", hidden: true, onchange: async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    toast(LANG === "ar" ? "جارٍ نسخ النموذج…" : "Copying model…");
    const r = await guard(() => api("POST", "/models/import", fd)); if (r) { toast("✓ " + r.name); viewSettings("models"); }
  } });
  body.append(
    h("div", { class: "card" }, h("h2", {}, t("مثبت")), installedBox,
      h("div", { class: "row", style: "margin-top:10px" }, h("button", { class: "btn", onclick: () => imp.click() }, t("استيراد ملف نموذج")), imp,
        h("span", { class: "hint ltr" }, m.folder))),
    h("div", { class: "card" }, h("h2", {}, LANG === "ar" ? "نماذج مقترحة (تُنزّل مرة واحدة ثم تعمل دون إنترنت)" : "Suggested models (download once, then offline)"),
      h("p", { class: "hint" }, LANG === "ar" ? "للأجهزة دون إنترنت: نزّل ملف ‎.gguf‎ على جهاز آخر وانقله بذاكرة USB ثم «استيراد ملف نموذج»." : "No internet? Download the .gguf elsewhere, copy it over USB and use Import."),
      catBox));
}

function settingsAnswer(body, s, save) {
  const a = s.answer;
  const strict = h("input", { type: "checkbox", checked: a.strict });
  const stream = h("input", { type: "checkbox", checked: a.stream });
  const sources = h("input", { class: "input", type: "number", min: 1, max: 20, value: a.max_sources });
  const temp = h("input", { class: "input", type: "number", step: "0.1", min: 0, max: 2, value: a.temperature });
  const maxTok = h("input", { class: "input", type: "number", min: 64, max: 8192, value: a.max_tokens });
  const hist = h("input", { class: "input", type: "number", min: 0, max: 10, value: a.history_turns });
  const sys = h("textarea", { class: "input", rows: 4, dir: "auto", value: a.system_prompt || "" });
  const lang = h("select", {}, h("option", { value: "ar", selected: s.ui_language === "ar" }, "العربية"), h("option", { value: "en", selected: s.ui_language === "en" }, "English"));
  body.append(h("div", { class: "card" },
    h("label", { class: "row" }, strict, h("span", {}, LANG === "ar" ? "الوضع الصارم: لا يجيب إلا من المستندات، وكل إجابة يجب أن تحيل إلى مصدر وإلا تُرفض" : "Strict mode: answer only from documents; uncited answers are refused")),
    h("p", { class: "hint" }, LANG === "ar" ? "في الوضع الصارم لا يظهر نص الإجابة أثناء الكتابة حتى تظهر أول إحالة صحيحة." : "In strict mode streamed text appears only after the first valid citation."),
    h("label", { class: "row", style: "margin:10px 0" }, stream, h("span", {}, LANG === "ar" ? "عرض الإجابة أثناء كتابتها" : "Stream answers")),
    h("div", { class: "row" },
      h("label", { class: "field grow" }, h("span", {}, LANG === "ar" ? "عدد المصادر لكل سؤال" : "Sources per question"), sources),
      h("label", { class: "field grow" }, h("span", {}, LANG === "ar" ? "درجة الإبداع (0 = أدق)" : "Temperature"), temp),
      h("label", { class: "field grow" }, h("span", {}, LANG === "ar" ? "أقصى طول للإجابة (رموز)" : "Max answer tokens"), maxTok),
      h("label", { class: "field grow" }, h("span", {}, LANG === "ar" ? "أسئلة سابقة تُرسل كسياق" : "History turns"), hist)),
    h("label", { class: "field" }, h("span", {}, LANG === "ar" ? "تعليمات إضافية للمساعد (اختياري)" : "Extra instructions (optional)"), sys),
    h("label", { class: "field" }, h("span", {}, LANG === "ar" ? "لغة الواجهة" : "Interface language"), lang),
    h("button", { class: "btn primary", onclick: async () => {
      const r = await save({ ui_language: lang.value, answer: { strict: strict.checked, stream: stream.checked, max_sources: Number(sources.value),
        temperature: Number(temp.value), max_tokens: Number(maxTok.value), history_turns: Number(hist.value), system_prompt: sys.value } });
      if (r && r.ui_language !== LANG) location.reload();
    } }, t("حفظ"))));
}

function settingsOCR(body, s, save) {
  const o = s.ocr, c = s.chunking;
  const num = (v, step, min, max) => h("input", { class: "input", type: "number", step, min, max, value: v });
  const auto = num(o.auto_approve_confidence, "0.01", 0, 1), minLine = num(o.min_line_score, "0.05", 0, 1);
  const scale = num(o.pdf_scale, "0.25", 1, 4), force = h("input", { type: "checkbox", checked: o.force_ocr });
  const size = num(c.size, "50", 200, 4000), overlap = num(c.overlap, "10", 0, 1000), low = num(c.low_confidence, "0.01", 0, 1);
  body.append(h("div", { class: "card" },
    h("h2", {}, t("التعرّف الضوئي")),
    h("p", { class: "hint" }, LANG === "ar" ? "يعمل التعرّف الضوئي العربي على هذا الجهاز دون إنترنت. ملفات PDF الرقمية تُقرأ مباشرة دون تعرّف ضوئي." : "Arabic OCR runs on this computer. Digital PDFs are read directly."),
    h("label", { class: "field" }, h("span", {}, LANG === "ar" ? "اعتماد تلقائي فوق هذه الثقة (وإلا يُطلب منك المراجعة)" : "Auto-approve above this confidence"), auto),
    h("label", { class: "field" }, h("span", {}, LANG === "ar" ? "تجاهل الأسطر الأضعف من" : "Drop lines below"), minLine),
    h("label", { class: "field" }, h("span", {}, LANG === "ar" ? "دقة تحويل صفحات PDF (2 = موصى به)" : "PDF render scale (2 recommended)"), scale),
    h("label", { class: "row", style: "margin-bottom:12px" }, force, h("span", {}, LANG === "ar" ? "استخدم التعرّف الضوئي دائماً حتى لملفات PDF الرقمية" : "Always OCR, even digital PDFs")),
    h("h2", {}, LANG === "ar" ? "التقطيع" : "Chunking"),
    h("div", { class: "row" },
      h("label", { class: "field grow" }, h("span", {}, LANG === "ar" ? "حجم المقطع (حرف)" : "Chunk size"), size),
      h("label", { class: "field grow" }, h("span", {}, LANG === "ar" ? "التداخل" : "Overlap"), overlap),
      h("label", { class: "field grow" }, h("span", {}, LANG === "ar" ? "علّم المقطع للمراجعة دون ثقة" : "Flag chunk below"), low)),
    h("button", { class: "btn primary", onclick: () => save({ ocr: { auto_approve_confidence: Number(auto.value), min_line_score: Number(minLine.value), pdf_scale: Number(scale.value), force_ocr: force.checked },
      chunking: { size: Number(size.value), overlap: Number(overlap.value), low_confidence: Number(low.value) } }) }, t("حفظ"))));
}

async function settingsData(body, s) {
  const cols = await api("GET", "/collections");
  const st = await api("GET", "/bootstrap");
  const sel = h("div", { class: "multisel" }, cols.map((c) => h("label", {}, h("input", { type: "checkbox", value: c.id, checked: true }), c.name)));
  body.append(h("div", { class: "card" }, h("h2", {}, LANG === "ar" ? "تصدير البيانات (RAG)" : "Export data (RAG)"),
    h("p", { class: "hint" }, LANG === "ar" ? "ملف JSONL: سطر لكل مقطع مع المستند والصفحات والثقة، جاهز لأي نظام RAG أو تدريب." : "JSONL: one line per chunk with document, pages and confidence."),
    sel, h("div", { style: "margin-top:10px" }, h("button", { class: "btn primary", onclick: async () => {
      const ids = [...sel.querySelectorAll("input:checked")].map((x) => Number(x.value));
      const r = await guard(() => api("POST", "/export", { collection_ids: ids }));
      if (r) { const a = h("a", { href: fileUrl("/exports/" + encodeURIComponent(r.file)), download: r.file }); document.body.append(a); a.click(); a.remove(); }
    } }, LANG === "ar" ? "تصدير" : "Export"))),
    h("div", { class: "card" }, h("h2", {}, LANG === "ar" ? "مكان البيانات" : "Data location"),
      h("p", { class: "ltr" }, st.data_dir),
      h("p", { class: "hint" }, `${st.stats.documents} ${t("المستندات")} · ${st.stats.chunks} ${t("المقاطع")} · v${st.version}`)));
}

boot().catch((e) => {
  document.getElementById("view").replaceChildren(h("div", { class: "page" }, h("div", { class: "banner warn" },
    "تعذّر الاتصال بالتطبيق. أغلق النافذة وافتح SigmaCode Desk من جديد. / Could not reach the app: restart SigmaCode Desk. (" + e.message + ")")));
});
