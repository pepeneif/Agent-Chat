"use strict";
const $ = (id) => document.getElementById(id);
const state = { pending: [], lastAt: 0, timer: null, sending: false };

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ credentials: "same-origin" }, opts || {}));
  let j = null;
  try { j = await r.json(); } catch (e) { j = null; }
  return { ok: r.ok, status: r.status, data: j };
}

/* ---------------- login ---------------- */
function showLogin() {
  $("loginView").classList.remove("hidden");
  $("topBar").classList.add("hidden");
  $("scroll").classList.add("hidden");
  $("composer").classList.add("hidden");
  $("typing").classList.add("hidden");
}
function showChat() {
  $("loginView").classList.add("hidden");
  $("topBar").classList.remove("hidden");
  $("scroll").classList.remove("hidden");
  $("composer").classList.remove("hidden");
  loadHistory().then(startPolling);
}

async function requestToken() {
  $("err").textContent = "";
  const b = $("btnToken"); b.disabled = true; b.textContent = "Enviando...";
  const r = await api("/api/request-token", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({}) });
  b.disabled = false; b.textContent = "Enviarme el codigo por email";
  if (!r.ok) { $("err").textContent = "No se pudo enviar. Intenta de nuevo."; return; }
  $("ok1").classList.remove("hidden");
  $("step2").classList.remove("hidden");
  $("tok").focus();
}
async function verify() {
  $("err").textContent = "";
  const t = $("tok").value.trim();
  if (!t) { $("err").textContent = "Pega el codigo."; return; }
  const b = $("btnVerify"); b.disabled = true; b.textContent = "Comprobando...";
  const r = await api("/api/verify", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ token: t }) });
  b.disabled = false; b.textContent = "Entrar";
  if (!r.ok) { $("err").textContent = "Codigo invalido o caducado."; return; }
  $("tok").value = "";
  showChat();
}
async function logout() {
  await api("/api/logout", { method: "POST" });
  stopPolling();
  showLogin();
  $("step2").classList.add("hidden");
  $("ok1").classList.add("hidden");
}

/* ---------------- render ---------------- */
function esc(s) { return (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
function fmtTime(ms) {
  const d = new Date(ms);
  return d.toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}
function linkify(text) {
  let h = esc(text);
  h = h.replace(/(\/api\/file\/[0-9a-f]{32})/g, (m) => `<a class="file" href="${m}" target="_blank" rel="noopener">abrir adjunto</a>`);
  return h;
}
function render(m) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + (m.role === "user" ? "user" : m.role === "system" ? "system" : "bot");
  const who = m.role === "user" ? "Tu" : m.role === "bot" ? "Manager" : "";
  let inner = "";
  if (m.role !== "system") inner += `<div class="who">${who} · ${fmtTime(m.at)}</div>`;
  let body = linkify(m.content);
  wrap.innerHTML = `<div><div>${inner}</div><div class="bubble">${body}</div></div>`;
  // imagenes: si hay un adjunto imagen, mostrarlo inline
  const ids = (m.content.match(/\/api\/file\/[0-9a-f]{32}/g) || []);
  ids.forEach((u) => {
    const img = new Image();
    img.src = u; img.alt = "adjunto";
    img.onload = () => { wrap.querySelector(".bubble").appendChild(img); scrollDown(); };
  });
  return wrap;
}
function scrollDown() { const s = $("scroll"); s.scrollTop = s.scrollHeight; }
function append(m) { $("scroll").appendChild(render(m)); }

async function loadHistory() {
  const r = await api("/api/history?limit=80");
  if (r.status === 401) { showLogin(); return; }
  const s = $("scroll"); s.innerHTML = "";
  (r.data.messages || []).forEach((m) => { append(m); if (m.at > state.lastAt) state.lastAt = m.at; });
  scrollDown();
}

/* ---------------- polling ---------------- */
async function tick() {
  const r = await api("/api/history?limit=40");
  if (r.status === 401) { stopPolling(); showLogin(); return; }
  const msgs = (r.data.messages || []).filter((m) => m.at > state.lastAt);
  const hadBot = msgs.some((m) => m.role === "bot" || m.role === "system");
  msgs.sort((a, b) => a.at - b.at).forEach((m) => {
    append(m);
    if (m.at > state.lastAt) state.lastAt = m.at;
  });
  if (msgs.length) scrollDown();
  if (hadBot) { state.sending = false; setTyping(false); $("btnSend").disabled = false; }
}
function startPolling() { stopPolling(); state.timer = setInterval(tick, 1000); tick(); }
function stopPolling() { if (state.timer) clearInterval(state.timer); state.timer = null; }
function setTyping(on) { $("typing").classList.toggle("hidden", !on); scrollDown(); }

/* ---------------- composer ---------------- */
function autosize() {
  const t = $("text");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 180) + "px";
}
function chip(f) {
  const el = document.createElement("span");
  el.className = "chip";
  el.dataset.id = f.id;
  el.innerHTML = (f.mime.startsWith("image/") ? `<img src="${f.url}">` : "") +
    `<span>${esc(f.name)}</span><button type="button" aria-label="quitar">×</button>`;
  el.querySelector("button").onclick = () => { el.remove(); state.pending = state.pending.filter((x) => x.id !== f.id); };
  return el;
}
async function upload(file) {
  const fd = new FormData();
  fd.append("file", file, file.name);
  const r = await api("/api/upload", { method: "POST", body: fd });
  if (r.ok && r.data && r.data.id) {
    state.pending.push(r.data);
    $("attachrow").appendChild(chip(r.data));
  } else {
    alert("No se pudo subir el archivo.");
  }
}
async function send() {
  if (state.sending) return;
  const t = $("text").value;
  const atts = state.pending.map((f) => f.id);
  if (!t.trim() && !atts.length) return;
  state.sending = true;
  $("btnSend").disabled = true;
  const r = await api("/api/send", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ text: t, attachments: atts })
  });
  if (!r.ok) { state.sending = false; $("btnSend").disabled = false; alert("No se pudo enviar."); return; }
  $("text").value = ""; autosize();
  state.pending = []; $("attachrow").innerHTML = "";
  setTyping(true);
  setTimeout(tick, 600);
}

/* ---------------- push (avisos) ---------------- */
function b64ToU8(b64) {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}
async function enablePush(btn) {
  try {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      alert("Este navegador no soporta avisos. En iPhone, abre desde el icono de la pantalla de inicio.");
      return;
    }
    const perm = await Notification.requestPermission();
    if (perm !== "granted") { alert("Permiso de avisos denegado. Actívalo en Ajustes del iPhone."); return; }
    const reg = await navigator.serviceWorker.ready;
    const kr = await api("/api/push/key");
    if (!kr.ok || !kr.data || !kr.data.key) { alert("El servidor no tiene push configurado."); return; }
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: b64ToU8(kr.data.key),
      });
    }
    const r = await api("/api/push/subscribe", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ subscription: sub.toJSON() }),
    });
    if (r.ok) {
      if (btn) { btn.textContent = "Avisos ON"; btn.classList.add("on"); }
      showBanner(false);
      api("/api/push/test", { method: "POST" });
    }
    else alert("No se pudo registrar el dispositivo.");
  } catch (e) {
    alert("Error activando avisos: " + e.message);
  }
}
function showBanner(on, txt) {
  const b = $("pushBanner");
  if (!b) return;
  if (txt) b.querySelector("span").textContent = txt;
  b.classList.toggle("hidden", !on);
}
async function pushState(btn) {
  try {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      btn.textContent = "Avisos n/d";
      showBanner(true, "Este iPhone no expone avisos aqui. Abre el chat desde el icono de la pantalla de inicio.");
      return;
    }
    if (Notification.permission === "denied") {
      btn.textContent = "Avisos bloqueado";
      showBanner(true, "Avisos bloqueados. Ve a Ajustes > Notificaciones > Chat y activalos.");
      return;
    }
    if (Notification.permission !== "granted") {
      btn.textContent = "Avisos";
      showBanner(true, "Activa los avisos para que te suene el movil cuando te escriba.");
      return;
    }
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) { btn.textContent = "Avisos ON"; btn.classList.add("on"); showBanner(false); }
    else { btn.textContent = "Avisos"; showBanner(true, "Falta un ultimo toque: activa los avisos."); }
  } catch (e) { btn.textContent = "Avisos error"; showBanner(true, "Error activando avisos: " + e.message); }
}

/* ---------------- wire ---------------- */
window.addEventListener("DOMContentLoaded", async () => {
  $("btnToken").onclick = requestToken;
  $("btnResend").onclick = requestToken;
  $("btnVerify").onclick = verify;
  $("tok").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); verify(); } });
  $("btnLogout").onclick = logout;
  $("btnReload").onclick = () => loadHistory().then(scrollDown);
  $("btnAttach").onclick = () => $("file").click();
  $("file").addEventListener("change", async (e) => {
    for (const f of Array.from(e.target.files || [])) await upload(f);
    e.target.value = "";
  });
  const ta = $("text");
  ta.addEventListener("input", autosize);
  // CRITICO: Enter NO envia; solo inserta linea nueva.
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); document.execCommand("insertLineBreak"); setTimeout(autosize, 0); }
  });
  // pegar imagenes del portapapeles
  ta.addEventListener("paste", async (e) => {
    const items = (e.clipboardData && e.clipboardData.items) || [];
    for (const it of items) {
      if (it.type && it.type.startsWith("image/")) {
        const f = it.getAsFile();
        if (f) { e.preventDefault(); await upload(new File([f], "pegado-" + Date.now() + ".png", { type: it.type })); }
      }
    }
  });
  $("btnSend").onclick = send;
  $("btnNotify").onclick = () => enablePush($("btnNotify"));
  $("btnPushGo").onclick = () => enablePush($("btnNotify"));

  const me = await api("/api/me");
  if (me.ok && me.data && me.data.ok) showChat(); else showLogin();
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pushState($("btnNotify"));
  });

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").then(() => pushState($("btnNotify"))).catch(() => {});
  }
});
