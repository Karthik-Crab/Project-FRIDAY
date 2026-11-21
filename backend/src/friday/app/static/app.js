"use strict";
// FRIDAY chat app. Plain JavaScript, no build step, works offline.

const $ = (id) => document.getElementById(id);
const els = {
  list: $("conv-list"), messages: $("messages"), empty: $("empty"), thread: $("thread"),
  input: $("input"), send: $("send"), composer: $("composer"), title: $("title"),
  voice: $("voice-toggle"), voiceLabel: $("voice-label"), voiceBar: $("voice-bar"),
  voiceStatus: $("voice-status"), voicePartial: $("voice-partial"), models: $("models"),
  sidebar: $("sidebar"), toast: $("toast"), chips: $("chips"), hint: $("hint"),
  tray: $("tray"), attach: $("attach"), fileInput: $("file-input"), drop: $("drop"),
  think: $("think-toggle"),
};

const state = {
  current: null,          // conversation id
  conversations: [],
  messages: new Map(),    // id -> message (current conversation)
  drafts: new Map(),      // draft id -> {el, text}
  mode: "chat",
  status: { can_make: [], voice: "off", voice_available: false, models: [] },
  ws: null,
  requests: new Map(),
  nextRequest: 1,
  timers: new Map(),
  attachments: [],        // files picked for the next message: {key, id, name, category, uploading, preview}
  think: loadThink(),     // let the talking model think before answering
  answering: null,         // conversation whose answer is being written ("new" before it has an id)
  waiting: null,          // "FRIDAY is working" bubble shown until the reply starts: {el, label, start, timer}
};

function loadThink() {
  try { return localStorage.getItem("friday.think") === "on"; } catch { return false; }
}

const SPOKEN = { image: "picture", music: "music", audio_edit: "song" };
const ICON = { image: "🖼", audio: "🎵", document: "📄" };
const KIND_OF = (name) => {
  const ext = (name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
  if ([".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"].includes(ext)) return "image";
  if ([".mp3", ".wav", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac"].includes(ext)) return "audio";
  return "document";
};
const PLACEHOLDER = {
  chat: "Message FRIDAY…",
  image: "Describe the picture… e.g. a white cat on a garden fence in the rain",
  music: "Describe the music… e.g. calm lo-fi piano, rainy evening",
};
const SUGGESTIONS = [
  ["chat", "What can you do?"],
  ["image", "A lighthouse on a cliff during a storm, cinematic"],
  ["music", "Gentle acoustic guitar, sunny morning"],
  ["chat", "Give me a fun fact about space"],
];

// ---- small helpers -------------------------------------------------------

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Enough Markdown for chat replies. Everything is escaped first.
function markdown(src) {
  const blocks = [];
  let text = esc(src).replace(/```[^\n]*\n?([\s\S]*?)(```|$)/g, (_, code) => {
    blocks.push(`<pre><code>${code.replace(/\n$/, "")}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  const inline = (s) => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out = [];
  let list = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  let para = [];
  const closePara = () => { if (para.length) { out.push(`<p>${inline(para.join("<br>"))}</p>`); para = []; } };
  for (const line of text.split("\n")) {
    const block = line.match(/^\u0000(\d+)\u0000$/);
    const head = line.match(/^(#{1,3})\s+(.*)/);
    const bullet = line.match(/^\s*[-*•]\s+(.*)/);
    const num = line.match(/^\s*\d+[.)]\s+(.*)/);
    if (block) { closePara(); closeList(); out.push(blocks[+block[1]]); }
    else if (head) { closePara(); closeList(); out.push(`<h3>${inline(head[2])}</h3>`); }
    else if (bullet || num) {
      closePara();
      const want = bullet ? "ul" : "ol";
      if (list !== want) { closeList(); out.push(`<${want}>`); list = want; }
      out.push(`<li>${inline((bullet || num)[1])}</li>`);
    } else if (!line.trim()) { closePara(); closeList(); }
    else { closeList(); para.push(line); }
  }
  closePara(); closeList();
  return out.join("").replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
}

function toast(message) {
  els.toast.textContent = message;
  els.toast.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => els.toast.classList.add("hidden"), 5000);
}

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

function nearBottom() {
  const t = els.thread;
  return t.scrollHeight - t.scrollTop - t.clientHeight < 120;
}
function scrollDown(force) {
  if (force || nearBottom()) els.thread.scrollTop = els.thread.scrollHeight;
}

function timeAgo(t) {
  const s = Date.now() / 1000 - t;
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  return new Date(t * 1000).toLocaleDateString();
}

// ---- live connection -----------------------------------------------------

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  state.ws = ws;
  ws.onmessage = (e) => onEvent(JSON.parse(e.data));
  ws.onopen = () => { connect.delay = 500; };
  ws.onclose = () => {
    state.ws = null;
    connect.delay = Math.min((connect.delay || 500) * 2, 8000);
    setTimeout(connect, connect.delay);
  };
}

function send(message) {
  return new Promise((resolve, reject) => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
      reject(new Error("Not connected to FRIDAY. Is the app still running?"));
      return;
    }
    const request_id = state.nextRequest++;
    state.requests.set(request_id, { resolve, reject });
    state.ws.send(JSON.stringify({ ...message, request_id }));
  });
}

function onEvent(ev) {
  switch (ev.type) {
    case "reply": {
      const r = state.requests.get(ev.request_id);
      state.requests.delete(ev.request_id);
      if (r) r.resolve(ev);
      break;
    }
    case "error": {
      const r = state.requests.get(ev.request_id);
      state.requests.delete(ev.request_id);
      if (r) r.reject(new Error(ev.message)); else toast(ev.message);
      break;
    }
    case "status": applyStatus(ev); break;
    case "conversation": upsertConversation(ev.conversation); break;
    case "conversation_deleted":
      state.conversations = state.conversations.filter((c) => c.id !== ev.id);
      renderList();
      if (state.current === ev.id) openConversation(null);
      break;
    case "message":
    case "message_update":
      if (ev.type === "message" && ev.message.role === "assistant") {
        clearWaiting();
        if (state.answering === ev.message.conversation_id || state.answering === "new") setAnswering(null);
      }
      touch(ev.message.conversation_id);
      if (ev.message.conversation_id === state.current) putMessage(ev.message);
      break;
    case "message_deleted": {
      state.messages.delete(ev.id);
      document.querySelector(`[data-id="${ev.id}"]`)?.remove();
      break;
    }
    case "stream_start":
      if (state.answering) setAnswering(ev.conversation_id);
      if (ev.conversation_id === state.current) startDraft(ev.draft);
      break;
    case "delta": {
      const d = state.drafts.get(ev.draft);
      if (d) {
        if (!d.text) { stopWaitingTimer(d); d.body.classList.add("cursor"); }
        d.text += ev.text;
        d.body.innerHTML = markdown(d.text);
        scrollDown();
      }
      break;
    }
    case "stream_end": {
      if (state.answering === ev.message.conversation_id) setAnswering(null);
      stopWaitingTimer(state.drafts.get(ev.draft));
      const d = state.drafts.get(ev.draft);
      state.drafts.delete(ev.draft);
      d?.el.remove();
      touch(ev.message.conversation_id);
      if (ev.message.conversation_id === state.current) putMessage(ev.message);
      break;
    }
    case "voice_state": showVoiceState(ev.state); break;
    case "voice_partial":
      els.voicePartial.textContent = ev.text ? `“${ev.text}”` : "";
      break;
  }
}

// ---- conversations -------------------------------------------------------

function upsertConversation(conv) {
  const i = state.conversations.findIndex((c) => c.id === conv.id);
  if (i >= 0) state.conversations[i] = conv; else state.conversations.unshift(conv);
  state.conversations.sort((a, b) => b.updated - a.updated);
  renderList();
  if (conv.id === state.current) els.title.textContent = conv.title;
}

function touch(id) {
  const c = state.conversations.find((x) => x.id === id);
  if (c) { c.updated = Date.now() / 1000; state.conversations.sort((a, b) => b.updated - a.updated); renderList(); }
}

function renderList() {
  els.list.innerHTML = "";
  for (const c of state.conversations) {
    const a = document.createElement("a");
    a.className = "conv" + (c.id === state.current ? " active" : "");
    a.href = `#/c/${c.id}`;
    a.title = c.title;
    const live = state.status.voice !== "off" && state.status.voice_conversation === c.id;
    a.innerHTML = `${live ? '<span class="live" title="Voice is on in this chat"></span>' : ""}
      ${c.incognito ? '<span class="conv-incognito" title="Incognito chat">🕶</span>' : ""}<span class="conv-title">${esc(c.title)}</span>
      <span class="conv-actions">
        <button class="mini-btn" data-act="rename" title="Rename">✎</button>
        <button class="mini-btn" data-act="delete" title="Delete">🗑</button>
      </span>`;
    a.addEventListener("click", (e) => {
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (!act) { els.sidebar.classList.remove("open"); return; }
      e.preventDefault();
      if (act === "rename") renameConversation(c);
      if (act === "delete") deleteConversation(c);
    });
    els.list.appendChild(a);
  }
}

async function renameConversation(c) {
  const title = prompt("Rename this chat", c.title);
  if (title === null) return;
  await api(`/api/conversations/${c.id}`, { method: "PATCH", body: JSON.stringify({ title }) });
}

async function deleteConversation(c) {
  const warning = c.incognito
    ? `Delete the incognito chat “${c.title}”? Everything in it is removed for good: messages, attached files, pictures and songs made in it.`
    : `Delete “${c.title}”? Pictures, music and changed songs stay saved on the computer.`;
  if (!confirm(warning)) return;
  await api(`/api/conversations/${c.id}`, { method: "DELETE" });
}

function showIncognito(on) {
  $("incognito-note").classList.toggle("hidden", !on);
  document.body.classList.toggle("incognito", on);
}

// A new chat on the computer (incognito if "New incognito chat" was pressed).
async function newConversation() {
  const conv = await api("/api/conversations", {
    method: "POST",
    body: JSON.stringify({ incognito: Boolean(state.nextIncognito) }),
  });
  upsertConversation(conv);
  await openConversation(conv.id);
  return conv.id;
}

async function openConversation(id, incognito = false) {
  state.current = id;
  state.nextIncognito = !id && incognito;
  showIncognito(!id && incognito);
  state.messages.clear();
  clearWaiting();
  for (const d of state.drafts.values()) stopWaitingTimer(d);
  state.drafts.clear();
  els.messages.innerHTML = "";
  if (!id) {
    els.title.textContent = incognito ? "New incognito chat" : "New chat";
    if (location.hash) history.replaceState(null, "", location.pathname);
  } else {
    if (location.hash !== `#/c/${id}`) history.replaceState(null, "", `#/c/${id}`);
    showIncognito(Boolean(state.conversations.find((c) => c.id === id)?.incognito));
    try {
      const data = await api(`/api/conversations/${id}`);
      if (state.current !== id) return;
      els.title.textContent = data.conversation.title;
      showIncognito(Boolean(data.conversation.incognito));
      for (const m of data.messages) putMessage(m);
    } catch {
      toast("That chat no longer exists.");
      return openConversation(null);
    }
  }
  renderList();
  updateEmpty();
  scrollDown(true);
  els.input.focus();
}

function updateEmpty() {
  const empty = state.messages.size === 0 && state.drafts.size === 0;
  els.empty.classList.toggle("hidden", !empty);
}

// ---- messages ------------------------------------------------------------

function putMessage(m) {
  state.messages.set(m.id, m);
  const el = renderMessage(m);
  const old = els.messages.querySelector(`[data-id="${m.id}"]`);
  const atBottom = nearBottom();
  if (old) old.replaceWith(el); else els.messages.appendChild(el);
  updateEmpty();
  if (atBottom || !old) scrollDown(!old);
}

function renderMessage(m) {
  const el = document.createElement("div");
  el.dataset.id = m.id;
  el.className = `msg ${m.role}`;
  if (m.role === "user") {
    const badge = m.meta.make ? `<span class="pill">${SPOKEN[m.meta.make] === "picture" ? "Picture" : "Music"}</span>` : "";
    const voice = m.meta.voice ? "<span>🎙 spoken</span>" : "";
    const files = (m.meta.files || []).map((f) => {
      const url = `/uploads/${encodeURIComponent(f.id)}`;
      if (f.category === "image") return `<a href="${url}" target="_blank"><img src="${url}" alt="${esc(f.name)}" loading="lazy"></a>`;
      if (f.category === "audio") return `<div class="doc">🎵 ${esc(f.name)}</div><audio controls preload="metadata" src="${url}"></audio>`;
      return `<a class="doc" href="${url}" download="${esc(f.name)}">📄 ${esc(f.name)}</a>`;
    }).join("");
    el.innerHTML = `${files ? `<div class="files">${files}</div>` : ""}${m.content ? `<div class="bubble">${esc(m.content)}</div>` : ""}${badge || voice ? `<div class="tag">${badge}${voice}</div>` : ""}`;
    return el;
  }
  if (m.kind === "proposal") return renderProposal(el, m);
  if (m.kind === "job") return renderJob(el, m);
  if (m.meta.failed) el.classList.add("failed");
  const who = [m.meta.stopped ? "⏹ stopped" : "", m.meta.voice ? "🎙 spoken" : "", m.meta.model || m.meta.provider || ""].filter(Boolean);
  el.innerHTML = `<div class="body">${markdown(m.content)}</div>${who.length ? `<div class="tag">${who.map(esc).map((w) => `<span>${w}</span>`).join("")}</div>` : ""}`;
  return el;
}

function renderProposal(el, m) {
  const what = SPOKEN[m.meta.kind] || "picture";
  const status = m.meta.status;
  if (status !== "pending") {
    el.innerHTML = `<div class="card"><div class="card-head">${status === "accepted" ? "✓ Making" : "✕ Not made"} · ${what}</div><p class="prompt">${esc(m.content || "—")}</p></div>`;
    return el;
  }
  el.innerHTML = `<div class="card">
    <div class="card-head"><b>Make this ${what}?</b> You can change the words first.</div>
    <textarea>${esc(m.content)}</textarea>
    <div class="card-actions">
      <button class="btn primary" data-act="make">Make it</button>
      <button class="btn" data-act="no">No thanks</button>
    </div></div>`;
  const box = el.querySelector("textarea");
  if (!m.content) setTimeout(() => box.focus(), 0);
  el.querySelector('[data-act="make"]').onclick = async () => {
    const promptText = box.value.trim();
    if (!promptText) { box.focus(); return; }
    try {
      await send({ type: "make", conversation_id: m.conversation_id, kind: m.meta.kind, prompt: promptText, proposal_id: m.id });
    } catch (e) { toast(e.message); }
  };
  el.querySelector('[data-act="no"]').onclick = () => send({ type: "dismiss", message_id: m.id }).catch((e) => toast(e.message));
  return el;
}

function renderJob(el, m) {
  const what = SPOKEN[m.meta.kind] || "picture";
  const st = m.meta.status;
  clearInterval(state.timers.get(m.id));
  state.timers.delete(m.id);
  let inner = `<div class="card-head">`;
  if (st === "running" || st === "waiting") {
    const doing = what === "song" ? "Changing the song" : m.meta.edit ? "Changing the picture" : `Making the ${what}`;
    inner += `<span class="spinner"></span><b>${st === "waiting" ? "Waiting for the previous job" : doing}</b><span class="elapsed"></span>`;
  } else if (st === "done") {
    inner += `<b>${m.meta.edit ? "Changed picture" : { picture: "Picture", music: "Music", song: "Changed song" }[what]}</b>`;
  } else if (st === "cancelled") {
    inner += `<b>Cancelled</b> · ${what}`;
  } else {
    inner += `<b>${what === "song" ? "Couldn't change the song" : m.meta.edit ? "Couldn't change the picture" : `Couldn't make the ${what}`}</b>`;
  }
  inner += `</div><p class="prompt">${esc(m.content)}</p>`;
  if (st === "running" || st === "waiting") {
    inner += `<div class="progress"></div><div class="card-actions"><button class="btn" data-act="cancel">Cancel</button></div>`;
  } else if (st === "done" && m.file_name) {
    const url = `/files/${m.id}`;
    inner += m.meta.kind === "music" || m.meta.kind === "audio_edit"
      ? `<audio controls preload="metadata" src="${url}"></audio>`
      : `<a href="${url}" target="_blank"><img src="${url}" alt="${esc(m.content)}" loading="lazy"></a>`;
    inner += `<div class="meta">${m.meta.seconds ? `<span>${m.meta.seconds} s</span>` : ""}<a href="${url}" download="${esc(m.file_name)}">Download</a><span>${esc(m.file_name)}</span></div>`;
    if (m.meta.note) inner += `<p class="note">${esc(m.meta.note)}</p>`;
  } else if (st === "failed") {
    inner += `<div class="error">${esc(m.meta.error || "")}</div>`;
  }
  el.innerHTML = `<div class="card">${inner}</div>`;
  const cancel = el.querySelector('[data-act="cancel"]');
  if (cancel) cancel.onclick = () => send({ type: "cancel_job", message_id: m.id }).catch((e) => toast(e.message));
  const elapsed = el.querySelector(".elapsed");
  if (elapsed && st === "running") {
    const begun = Date.now();
    const tick = () => { elapsed.textContent = ` · ${Math.round((Date.now() - begun) / 1000)} s`; };
    tick();
    state.timers.set(m.id, setInterval(tick, 1000));
  }
  return el;
}

function waitingHtml(label) {
  return `<span class="working" aria-live="polite"><span class="orb" aria-hidden="true"></span>`
    + `<span class="typing" aria-hidden="true"><i></i><i></i><i></i></span>`
    + `<span class="wait-label">${esc(label || "Writing…")}</span><span class="wait-time"></span></span>`;
}

// While an answer is being written, the send button becomes Stop.
function setAnswering(cid) {
  state.answering = cid;
  const on = Boolean(cid);
  els.send.classList.toggle("stop", on);
  els.send.textContent = on ? "■" : "↑";
  els.send.setAttribute("aria-label", on ? "Stop the answer" : "Send");
  els.send.title = on ? "Stop the answer (Esc)" : "";
  autosize();
}

async function stopAnswer() {
  const cid = state.answering;
  if (!cid || cid === "new") return;
  try { await send({ type: "stop", conversation_id: cid }); } catch (e) { toast(e.message); }
}

// A bubble with moving dots, shown from sending until the first words arrive.
function showWaiting(label) {
  clearWaiting();
  const el = document.createElement("div");
  el.className = "msg assistant waiting";
  el.innerHTML = `<div class="body">${waitingHtml(label)}</div>`;
  els.messages.appendChild(el);
  state.waiting = { el, label, start: Date.now(), timer: null };
  startWaitingTimer(state.waiting);
  updateEmpty();
  scrollDown(true);
}

function clearWaiting() {
  if (!state.waiting) return;
  stopWaitingTimer(state.waiting);
  state.waiting.el.remove();
  state.waiting = null;
}

// After a few seconds, show how long it has been, so a slow answer doesn't look stuck.
function startWaitingTimer(w) {
  w.timer = setInterval(() => {
    const s = Math.round((Date.now() - w.start) / 1000);
    const t = w.el.querySelector(".wait-time");
    if (t && s >= 3) t.textContent = ` ${s} s`;
  }, 1000);
}

function stopWaitingTimer(w) {
  if (w && w.timer) { clearInterval(w.timer); w.timer = null; }
}

function startDraft(id) {
  // Keep the waiting bubble (and its clock) if there is one; the reply goes in it.
  const w = state.waiting;
  state.waiting = null;
  let el, start, label;
  if (w) {
    ({ el, start, label } = w);
    stopWaitingTimer(w);
    el.classList.remove("waiting");
  } else {
    el = document.createElement("div");
    el.className = "msg assistant";
    start = Date.now();
    label = "";
    el.innerHTML = `<div class="body">${waitingHtml(label)}</div>`;
    els.messages.appendChild(el);
  }
  const d = { el, body: el.querySelector(".body"), text: "", start, timer: null };
  startWaitingTimer(d);
  state.drafts.set(id, d);
  updateEmpty();
  scrollDown(true);
}

// ---- sending -------------------------------------------------------------

async function submit(text) {
  text = text.trim();
  const files = state.attachments;
  if (files.some((a) => a.uploading)) { toast("Wait a moment: the file is still being attached."); return; }
  if (!text && !files.length) return;
  els.input.value = "";
  state.attachments = [];
  renderTray();
  autosize();
  const mode = files.length ? "chat" : state.mode;
  try {
    let reply;
    if (mode === "chat") {
      const kinds = files.map((a) => a.category);
      const think = state.think && state.status.can_think;
      if (!kinds.includes("audio")) {
        setAnswering(state.current || "new");
        showWaiting(
          kinds.includes("image") ? "Looking at the picture…"
            : kinds.includes("document") ? "Reading the document…"
            : think ? "Thinking…" : "",
        );
      }
      reply = await sendWithConversation({ type: "chat", text, files: files.map((a) => a.id), think });
    } else {
      reply = await sendWithConversation({ type: "make", kind: mode, prompt: text });
    }
    return reply;
  } catch (e) {
    clearWaiting();
    setAnswering(null);
    toast(e.message);
    if (!els.input.value) { els.input.value = text; autosize(); }
    if (!state.attachments.length && files.length) { state.attachments = files; renderTray(); }
  }
}

// ---- attaching files -----------------------------------------------------

async function attachFiles(list) {
  if (!state.status.can_attach) { toast("Attaching files isn't set up."); return; }
  const can = state.status.can_read || {};
  for (const file of list) {
    const category = KIND_OF(file.name);
    if (category === "image" && can.image === false) toast("Reading pictures isn't set up, so FRIDAY can't see this one.");
    if (category === "audio" && can.audio === false) toast("Changing songs needs ffmpeg, which isn't installed.");
    const a = {
      key: Math.random().toString(36).slice(2), id: null, name: file.name, category, uploading: true,
      preview: category === "image" ? URL.createObjectURL(file) : null,
    };
    state.attachments.push(a);
    renderTray();
    try {
      const res = await fetch(`/api/upload?name=${encodeURIComponent(file.name)}`, {
        method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || res.statusText);
      Object.assign(a, data, { uploading: false });
    } catch (e) {
      toast(`${file.name}: ${e.message}`);
      state.attachments = state.attachments.filter((x) => x !== a);
    }
    renderTray();
  }
  if (state.mode !== "chat") setMode("chat");
  els.input.focus();
}

function renderTray() {
  els.tray.innerHTML = "";
  els.tray.classList.toggle("hidden", !state.attachments.length);
  for (const a of state.attachments) {
    const el = document.createElement("div");
    el.className = "att" + (a.uploading ? " uploading" : "");
    const pic = a.preview ? `<img src="${a.preview}" alt="">` : `<span class="ico">${ICON[a.category] || "📄"}</span>`;
    const sub = a.uploading ? "attaching…" : { image: "picture", audio: "song", document: "document" }[a.category];
    el.innerHTML = `${pic}<div style="min-width:0"><div class="name">${esc(a.name)}</div><div class="sub">${sub}</div></div><button type="button" class="x" title="Remove">✕</button>`;
    el.querySelector(".x").onclick = () => {
      state.attachments = state.attachments.filter((x) => x !== a);
      renderTray();
      autosize();
    };
    els.tray.appendChild(el);
  }
  autosize();
}

// A new chat gets its id from the server; then the page shows it.
async function sendWithConversation(message) {
  let id = state.current;
  if (!id) id = await newConversation();
  return send({ ...message, conversation_id: id });
}

function renderThink() {
  const show = !!state.status.can_think && state.mode === "chat";
  els.think.classList.toggle("hidden", !show);
  els.think.setAttribute("aria-pressed", state.think ? "true" : "false");
  els.think.title = state.think
    ? "Thinking is on: FRIDAY thinks before answering (slower, better for hard questions). Click to turn off."
    : "Thinking is off: FRIDAY answers straight away. Click to let her think first.";
}

function setMode(mode) {
  if (mode !== "chat" && !state.status.can_make.includes(mode)) {
    toast(`Making ${SPOKEN[mode]} isn't set up on this computer.`);
    return;
  }
  state.mode = mode;
  for (const b of document.querySelectorAll(".mode")) b.classList.toggle("active", b.dataset.mode === mode);
  renderThink();
  els.input.placeholder = PLACEHOLDER[mode];
  els.hint.textContent = mode === "chat"
    ? "Enter to send · Shift+Enter for a new line"
    : `Enter makes the ${SPOKEN[mode]} straight away`;
  els.input.focus();
}

function autosize() {
  els.input.style.height = "auto";
  els.input.style.height = Math.min(els.input.scrollHeight, 220) + "px";
  els.send.disabled = !state.answering && !els.input.value.trim() && !state.attachments.length;
}

// ---- voice ---------------------------------------------------------------

function applyStatus(s) {
  state.status = s;
  for (const b of document.querySelectorAll(".mode")) {
    const m = b.dataset.mode;
    b.classList.toggle("hidden", m !== "chat" && !s.can_make.includes(m));
  }
  if (state.mode !== "chat" && !s.can_make.includes(state.mode)) setMode("chat");
  els.models.innerHTML = (s.models || []).map((m) =>
    `<div class="model" title="${esc(m.model)}${m.where ? " · " + esc(m.where) : ""}">`
    + (m.role ? `<span class="model-role">${esc(m.role)}</span>` : "")
    + `<span class="model-name">${esc(m.label)}</span>`
    + (m.where ? `<span class="model-where">${esc(m.where)}</span>` : "")
    + `</div>`).join("");
  renderThink();
  els.voice.disabled = !s.voice_available || s.voice === "starting" || s.voice === "stopping";
  els.voice.title = s.voice_available ? "Talk to FRIDAY" : "Voice mode isn't set up";
  const on = s.voice === "on";
  els.voice.setAttribute("aria-pressed", on ? "true" : "false");
  els.voice.classList.toggle("busy", s.voice === "starting" || s.voice === "stopping");
  els.voiceLabel.textContent = { starting: "Starting voice…", stopping: "Stopping…", on: "Voice on" }[s.voice] || "Voice";
  els.voiceBar.classList.toggle("hidden", !on);
  if (on) showVoiceState("listening");
  renderList();
}

function showVoiceState(st) {
  els.voiceStatus.textContent = {
    listening: "Listening…", user_speaking: "Hearing you…", thinking: "Thinking…", speaking: "Speaking…",
  }[st] || st;
}

async function toggleVoice() {
  const on = state.status.voice !== "on";
  try {
    if (on && !state.current) await newConversation();
    const reply = await send({ type: "voice", on, conversation_id: state.current });
    if (on && reply.conversation_id && reply.conversation_id !== state.current) openConversation(reply.conversation_id);
  } catch (e) { toast(e.message); }
}

// ---- wiring --------------------------------------------------------------

els.composer.addEventListener("submit", (e) => {
  e.preventDefault();
  if (state.answering) stopAnswer(); else submit(els.input.value);
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.answering) stopAnswer(); });
els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(els.input.value); }
});
els.input.addEventListener("input", autosize);
for (const b of document.querySelectorAll(".mode")) b.addEventListener("click", () => setMode(b.dataset.mode));
$("new-chat").addEventListener("click", () => { openConversation(null); els.sidebar.classList.remove("open"); });
$("new-incognito").addEventListener("click", () => { openConversation(null, true); els.sidebar.classList.remove("open"); });
$("open-sidebar").addEventListener("click", () => els.sidebar.classList.add("open"));
$("close-sidebar").addEventListener("click", () => els.sidebar.classList.remove("open"));
els.voice.addEventListener("click", toggleVoice);
els.think.addEventListener("click", () => {
  state.think = !state.think;
  try { localStorage.setItem("friday.think", state.think ? "on" : "off"); } catch { /* not kept */ }
  renderThink();
  toast(state.think
    ? "Thinking on: FRIDAY thinks before answering. Better for hard questions, but slower."
    : "Thinking off: FRIDAY answers straight away.");
});
els.attach.addEventListener("click", () => els.fileInput.click());
els.fileInput.addEventListener("change", () => { attachFiles([...els.fileInput.files]); els.fileInput.value = ""; });
els.input.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); attachFiles(files); }
});
let dragDepth = 0;
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
window.addEventListener("dragenter", (e) => { if (hasFiles(e)) { dragDepth++; els.drop.classList.remove("hidden"); } });
window.addEventListener("dragleave", (e) => { if (hasFiles(e) && --dragDepth <= 0) { dragDepth = 0; els.drop.classList.add("hidden"); } });
window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth = 0;
  els.drop.classList.add("hidden");
  attachFiles([...e.dataTransfer.files]);
});
window.addEventListener("hashchange", () => {
  const id = (location.hash.match(/^#\/c\/(\w+)/) || [])[1] || null;
  if (id !== state.current) openConversation(id);
});
for (const [mode, text] of SUGGESTIONS) {
  const b = document.createElement("button");
  b.className = "chip";
  b.textContent = (mode === "image" ? "🖼 " : mode === "music" ? "🎵 " : "") + text;
  b.onclick = () => { setMode(state.status.can_make.includes(mode) || mode === "chat" ? mode : "chat"); els.input.value = text; autosize(); els.input.focus(); };
  els.chips.appendChild(b);
}

(async function start() {
  connect();
  autosize();
  try {
    state.conversations = await api("/api/conversations");
    applyStatus(await api("/api/status"));
  } catch (e) { toast(e.message); }
  const id = (location.hash.match(/^#\/c\/(\w+)/) || [])[1] || null;
  openConversation(id && state.conversations.some((c) => c.id === id) ? id : null);
})();
