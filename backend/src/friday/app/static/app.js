"use strict";
// FRIDAY chat app. Plain JavaScript, no build step, works offline.

const $ = (id) => document.getElementById(id);
const els = {
  list: $("conv-list"), messages: $("messages"), empty: $("empty"), thread: $("thread"),
  input: $("input"), send: $("send"), composer: $("composer"), title: $("title"),
  voice: $("voice-toggle"), voiceLabel: $("voice-label"), voiceBar: $("voice-bar"),
  voiceStatus: $("voice-status"), voicePartial: $("voice-partial"), models: $("models"),
  sidebar: $("sidebar"), toast: $("toast"), chips: $("chips"), hint: $("hint"),
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
};

const SPOKEN = { image: "picture", music: "music" };
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
      touch(ev.message.conversation_id);
      if (ev.message.conversation_id === state.current) putMessage(ev.message);
      break;
    case "message_deleted": {
      state.messages.delete(ev.id);
      document.querySelector(`[data-id="${ev.id}"]`)?.remove();
      break;
    }
    case "stream_start":
      if (ev.conversation_id === state.current) startDraft(ev.draft);
      break;
    case "delta": {
      const d = state.drafts.get(ev.draft);
      if (d) {
        d.text += ev.text;
        d.body.innerHTML = markdown(d.text);
        scrollDown();
      }
      break;
    }
    case "stream_end": {
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
      <span class="conv-title">${esc(c.title)}</span>
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
  if (!confirm(`Delete “${c.title}”? Pictures and music stay saved on the computer.`)) return;
  await api(`/api/conversations/${c.id}`, { method: "DELETE" });
}

async function openConversation(id) {
  state.current = id;
  state.messages.clear();
  state.drafts.clear();
  els.messages.innerHTML = "";
  if (!id) {
    els.title.textContent = "New chat";
    if (location.hash) history.replaceState(null, "", location.pathname);
  } else {
    if (location.hash !== `#/c/${id}`) history.replaceState(null, "", `#/c/${id}`);
    try {
      const data = await api(`/api/conversations/${id}`);
      if (state.current !== id) return;
      els.title.textContent = data.conversation.title;
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
    el.innerHTML = `<div class="bubble">${esc(m.content)}</div>${badge || voice ? `<div class="tag">${badge}${voice}</div>` : ""}`;
    return el;
  }
  if (m.kind === "proposal") return renderProposal(el, m);
  if (m.kind === "job") return renderJob(el, m);
  if (m.meta.failed) el.classList.add("failed");
  const who = [m.meta.voice ? "🎙 spoken" : "", m.meta.model || m.meta.provider || ""].filter(Boolean);
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
    inner += `<span class="spinner"></span><b>${st === "waiting" ? "Waiting for the previous job" : `Making the ${what}`}</b><span class="elapsed"></span>`;
  } else if (st === "done") {
    inner += `<b>${what === "picture" ? "Picture" : "Music"}</b>`;
  } else if (st === "cancelled") {
    inner += `<b>Cancelled</b> · ${what}`;
  } else {
    inner += `<b>Couldn't make the ${what}</b>`;
  }
  inner += `</div><p class="prompt">${esc(m.content)}</p>`;
  if (st === "running" || st === "waiting") {
    inner += `<div class="progress"></div><div class="card-actions"><button class="btn" data-act="cancel">Cancel</button></div>`;
  } else if (st === "done" && m.file_name) {
    const url = `/files/${m.id}`;
    inner += m.meta.kind === "music"
      ? `<audio controls preload="metadata" src="${url}"></audio>`
      : `<a href="${url}" target="_blank"><img src="${url}" alt="${esc(m.content)}" loading="lazy"></a>`;
    inner += `<div class="meta">${m.meta.seconds ? `<span>${m.meta.seconds} s</span>` : ""}<a href="${url}" download="${esc(m.file_name)}">Download</a><span>${esc(m.file_name)}</span></div>`;
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

function startDraft(id) {
  const el = document.createElement("div");
  el.className = "msg assistant";
  el.innerHTML = `<div class="body cursor"></div>`;
  els.messages.appendChild(el);
  state.drafts.set(id, { el, body: el.querySelector(".body"), text: "" });
  updateEmpty();
  scrollDown(true);
}

// ---- sending -------------------------------------------------------------

async function submit(text) {
  text = text.trim();
  if (!text) return;
  els.input.value = "";
  autosize();
  const mode = state.mode;
  try {
    let reply;
    if (mode === "chat") {
      reply = await sendWithConversation({ type: "chat", text });
    } else {
      reply = await sendWithConversation({ type: "make", kind: mode, prompt: text });
    }
    return reply;
  } catch (e) {
    toast(e.message);
    if (!els.input.value) { els.input.value = text; autosize(); }
  }
}

// A new chat gets its id from the server; then the page shows it.
async function sendWithConversation(message) {
  let id = state.current;
  if (!id) {
    const conv = await api("/api/conversations", { method: "POST" });
    upsertConversation(conv);
    await openConversation(conv.id);
    id = conv.id;
  }
  return send({ ...message, conversation_id: id });
}

function setMode(mode) {
  if (mode !== "chat" && !state.status.can_make.includes(mode)) {
    toast(`Making ${SPOKEN[mode]} isn't set up on this computer.`);
    return;
  }
  state.mode = mode;
  for (const b of document.querySelectorAll(".mode")) b.classList.toggle("active", b.dataset.mode === mode);
  els.input.placeholder = PLACEHOLDER[mode];
  els.hint.textContent = mode === "chat"
    ? "Enter to send · Shift+Enter for a new line"
    : `Enter makes the ${SPOKEN[mode]} straight away`;
  els.input.focus();
}

function autosize() {
  els.input.style.height = "auto";
  els.input.style.height = Math.min(els.input.scrollHeight, 220) + "px";
  els.send.disabled = !els.input.value.trim();
}

// ---- voice ---------------------------------------------------------------

function applyStatus(s) {
  state.status = s;
  for (const b of document.querySelectorAll(".mode")) {
    const m = b.dataset.mode;
    b.classList.toggle("hidden", m !== "chat" && !s.can_make.includes(m));
  }
  if (state.mode !== "chat" && !s.can_make.includes(state.mode)) setMode("chat");
  els.models.innerHTML = (s.models || []).map((m) => `<div title="${esc(m.model)}">● ${esc(m.label)}</div>`).join("");
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
    if (on && !state.current) {
      const conv = await api("/api/conversations", { method: "POST" });
      upsertConversation(conv);
      await openConversation(conv.id);
    }
    const reply = await send({ type: "voice", on, conversation_id: state.current });
    if (on && reply.conversation_id && reply.conversation_id !== state.current) openConversation(reply.conversation_id);
  } catch (e) { toast(e.message); }
}

// ---- wiring --------------------------------------------------------------

els.composer.addEventListener("submit", (e) => { e.preventDefault(); submit(els.input.value); });
els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(els.input.value); }
});
els.input.addEventListener("input", autosize);
for (const b of document.querySelectorAll(".mode")) b.addEventListener("click", () => setMode(b.dataset.mode));
$("new-chat").addEventListener("click", () => { openConversation(null); els.sidebar.classList.remove("open"); });
$("open-sidebar").addEventListener("click", () => els.sidebar.classList.add("open"));
$("close-sidebar").addEventListener("click", () => els.sidebar.classList.remove("open"));
els.voice.addEventListener("click", toggleVoice);
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
