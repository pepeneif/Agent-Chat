"use strict";
const $ = (id) => document.getElementById(id);
const state = { pending: [], lastAt: 0, timer: null, sending: false, awaitOwn: false };
let OWNER_EMAIL = "";
// Fix 3 (zoom al escribir) REVERSIBLE: para volver al comportamiento anterior, cambia a false.
const FIX_COMPOSER_ZOOM = true;
const FIX_ZOOM = FIX_COMPOSER_ZOOM;   // alias
if (FIX_COMPOSER_ZOOM) document.documentElement.classList.add("fix-zoom");
else document.documentElement.classList.add("no-fix-zoom");

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ credentials: "same-origin" }, opts || {}));
  let j = null;
  try { j = await r.json(); } catch (e) { j = null; }
  return { ok: r.ok, status: r.status, data: j };
}

let _toastT = null;
function toast(msg, ms) {
  const el = $("toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.remove("hidden");
  if (_toastT) clearTimeout(_toastT);
  _toastT = setTimeout(() => el.classList.add("hidden"), ms || 4000);
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
  const r = await api("/api/request-token", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ email: OWNER_EMAIL }) });
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
  let r;
  try {
    r = await api("/api/verify", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ token: t }) });
  } catch (e) {
    r = null;
  }
  b.disabled = false; b.textContent = "Entrar";
  if (!r) { $("err").textContent = "No hubo conexion. Revisa tu red e intenta de nuevo."; return; }
  if (!r.ok) { $("err").textContent = "Codigo invalido o caducado. Pide uno nuevo."; return; }
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
function mdInline(s) {
  let h = esc(s);
  h = h.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  h = h.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  h = h.replace(/(^|\W)\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  return h;
}
function linkify(text) {
  const src = (text || "").replace(/\r\n?/g, "\n");
  const lines = src.split("\n");
  const out = [];
  const liRe = /^\s*[-\u2022]\s+(.*)$/;
  const olRe = /^\s*\d+[.)]\s+(.*)$/;
  const flushList = (tag, items) =>
    out.push("<" + tag + ">" + items.map((x) => "<li>" + mdInline(x) + "</li>").join("") + "</" + tag + ">");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*$/.test(line)) { i++; continue; }
    const hm = /^(#{1,6})\s+(.*)$/.exec(line);
    if (hm) { const lv = Math.min(hm[1].length, 3); out.push("<h" + lv + ">" + mdInline(hm[2]) + "</h" + lv + ">"); i++; continue; }
    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
    if (/^\s*>\s?/.test(line)) {
      const q = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) { q.push(lines[i].replace(/^\s*>\s?/, "")); i++; }
      out.push("<blockquote>" + mdInline(q.join("\n")).replace(/\n/g, "<br>") + "</blockquote>");
      continue;
    }
    if (liRe.test(line)) {
      const buf = [];
      while (i < lines.length) {
        const m = liRe.exec(lines[i]);
        if (m) { buf.push(m[1]); i++; }
        else if (/^\s+\S/.test(lines[i]) && buf.length) { buf[buf.length - 1] += " " + lines[i].trim(); i++; }
        else break;
      }
      flushList("ul", buf); continue;
    }
    if (olRe.test(line)) {
      const buf = [];
      while (i < lines.length) {
        const m = olRe.exec(lines[i]);
        if (m) { buf.push(m[1]); i++; }
        else if (/^\s+\S/.test(lines[i]) && buf.length) { buf[buf.length - 1] += " " + lines[i].trim(); i++; }
        else break;
      }
      flushList("ol", buf); continue;
    }
    const para = [line];
    i++;
    while (i < lines.length && !/^\s*$/.test(lines[i]) && !liRe.test(lines[i]) && !olRe.test(lines[i])
           && !/^\s*>/.test(lines[i]) && !/^(#{1,6})\s+/.test(lines[i]) && !/^\s*(---+|\*\*\*+)\s*$/.test(lines[i])) {
      para.push(lines[i]); i++;
    }
    out.push("<p>" + mdInline(para.join("\n")).replace(/\n/g, "<br>") + "</p>");
  }
  let html = out.join("\n");
  html = html.replace(/(\/api\/file\/[0-9a-f]{32})/g, (m) => `<a class="file" href="${m}" target="_blank" rel="noopener">abrir adjunto</a>`);
  return html;
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
    img.onload = () => { wrap.querySelector(".bubble").appendChild(img); if (_stickBottom) { scrollDown(); } };
  });
  return wrap;
}
function atBottom(s, slack) {
  if (!s) return true;
  return (s.scrollHeight - s.scrollTop - s.clientHeight) <= (slack == null ? 80 : slack);
}
function _forceBottom(s) {
  if (!s) return;
  const prev = s.style.scrollBehavior;
  s.style.scrollBehavior = "auto";   // salto inmediato: nada de scroll suave (causaba el desfase de ~90px)
  s.scrollTop = s.scrollHeight;
  s.style.scrollBehavior = prev || "";
}
// Reafirma el fondo tras un frame: el alto cambia cuando el layout/pintado terminan
// (imagenes, fuentes, markdown). Sin esto el chat quedaba ~90px corto y se "soltaba".
function pinBottom() {
  requestAnimationFrame(() => { const s = $("scroll"); if (s) _forceBottom(s); });
}
function scrollDown(force) {
  const s = $("scroll");
  if (!s) return;
  // _stickBottom: pegado al fondo. Solo se suelta si el usuario sube a mano (listener mas abajo).
  // Antes bastaba un desfase de 94px para que el chat se creyera "leyendo arriba" y se congelara.
  if (!force && !_stickBottom && !atBottom(s, 120)) return;
  _forceBottom(s);
  pinBottom();
}
let _stickBottom = true;

function scrollToTopOf(el, offset) {
  const s = $("scroll");
  if (!s || !el) return;
  const top = el.getBoundingClientRect().top - s.getBoundingClientRect().top + s.scrollTop;
  const target = Math.max(0, top - (offset == null ? 8 : offset));
  const prev = s.style.scrollBehavior;
  s.style.scrollBehavior = "auto";   // salto, sin animacion lenta
  s.scrollTop = target;
  s.style.scrollBehavior = prev || "";
}
function revealMessage(el, topPad, botPad) {
  // Si el mensaje CABE en la pantalla, alineamos su parte inferior con la inferior
  // (se ve entero). Si NO cabe, alineamos su parte superior con la superior
  // (se ve el principio, que es lo que se empieza a leer).
  const s = $("scroll");
  if (!s || !el) return;
  const sbox = s.getBoundingClientRect();
  const box = el.getBoundingClientRect();
  const topPadV = topPad == null ? 8 : topPad;
  const botPadV = botPad == null ? 12 : botPad;
  const fits = (box.height + topPadV + botPadV) <= s.clientHeight;
  const prev = s.style.scrollBehavior;
  s.style.scrollBehavior = "auto";   // salto, sin animacion lenta
  if (fits) {
    const bottom = box.bottom - sbox.top + s.scrollTop;
    s.scrollTop = Math.max(0, bottom - s.clientHeight + botPadV);
  } else {
    const top = box.top - sbox.top + s.scrollTop;
    s.scrollTop = Math.max(0, top - topPadV);
  }
  s.style.scrollBehavior = prev || "";
}

function scrollBottomOf(el, offset) {
  const s = $("scroll");
  if (!s || !el) return;
  const box = el.getBoundingClientRect();
  const sbox = s.getBoundingClientRect();
  const bottom = box.bottom - sbox.top + s.scrollTop;
  const target = bottom - s.clientHeight + (offset == null ? 10 : offset);
  const prev = s.style.scrollBehavior;
  s.style.scrollBehavior = "auto";
  s.scrollTop = Math.max(0, target);
  s.style.scrollBehavior = prev || "";
}
function append(m) {
  const el = render(m);
  el.dataset.at = String(m.at || 0);
  $("scroll").appendChild(el);
  return el;
}

async function loadHistory() {
  const r = await api("/api/history?limit=80");
  if (r.status === 401) { showLogin(); return; }
  const s = $("scroll"); s.innerHTML = "";
  (r.data.messages || []).forEach((m) => { append(m); if (m.at > state.lastAt) state.lastAt = m.at; });
  _stickBottom = true;               // una carga limpia siempre arranca pegado al ultimo mensaje
  scrollDown(true);
  pinBottom();                       // y lo reafirmamos tras el layout (imagenes/markdown)
  setTimeout(() => { const s2 = $("scroll"); if (s2 && _stickBottom) _forceBottom(s2); }, 300);
}

/* ---------------- polling ---------------- */
async function tick() {
  const r = await api("/api/history?limit=40");
  if (r.status === 401) { stopPolling(); showLogin(); return; }
  if (r.data && typeof r.data.busy === "boolean") setTyping(r.data.busy, r.data.busySince);
  const msgs = (r.data.messages || []).filter((m) => m.at > state.lastAt);
  const hadBot = msgs.some((m) => m.role === "bot" || m.role === "system");
  const near = atBottom($("scroll"), 120);  // medir ANTES de insertar
  if (near) _stickBottom = true;            // el usuario estaba abajo -> seguimos pegados
  const stick = _stickBottom;               // fuente de verdad: el flag, no un unico umbral
  msgs.sort((a, b) => a.at - b.at);
  const els = msgs.map((m) => {
    const el = append(m);
    if (m.at > state.lastAt) state.lastAt = m.at;
    return el;
  });
  if (state.awaitOwn) {
    const mine = msgs.findIndex((m) => m.role === "user");
    if (mine >= 0) { scrollBottomOf(els[mine]); state.awaitOwn = false; }
    else if (msgs.length && stick) scrollToTopOf(els[0]);
  } else if (msgs.length && stick) {
    revealMessage(els[els.length - 1]);   // anclar el mensaje MAS NUEVO, no el primero del lote
  }
  if (hadBot) { state.sending = false; $("btnSend").disabled = false; }
}
function startPolling() { stopPolling(); state.timer = setInterval(tick, 1000); tick(); }
function stopPolling() { if (state.timer) clearInterval(state.timer); state.timer = null; }
function setTyping(on, sinceMs) {
  const el = $("typing");
  if (on && sinceMs) {
    const secs = Math.max(0, Math.round((Date.now() - sinceMs) / 1000));
    el.textContent = secs > 3
      ? "Manager esta trabajando... (" + secs + "s)"
      : "Manager esta trabajando...";
  } else if (on) {
    el.textContent = "Manager esta trabajando...";
  }
  el.classList.toggle("hidden", !on);
}

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
  state.awaitOwn = true;       // al llegar, mi mensaje queda pegado abajo
  setTyping(true);
  setTimeout(tick, 300);
}

/* ---------------- push (avisos) ---------------- */
function b64ToU8(b64) {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}
async function disablePush(btn) {
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) {
      await api("/api/push/unsubscribe", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ endpoint: sub.endpoint }),
      });
      try { await api("/api/push/prefs", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ enabled: false }) }); } catch (e) {}
      try { await sub.unsubscribe(); } catch (e) {}
    }
    setNotifyBtn(btn, "off");
    showBanner(false);
  } catch (e) {
    alert("Error desactivando avisos: " + e.message);
  }
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
    // Siempre (re)registramos en el servidor: si iOS renovo el endpoint y el server
    // no lo tenia, esto lo vuelve a dar de alta (auto-reparable). Fix 4.
    const r = await api("/api/push/subscribe", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ subscription: sub.toJSON() }),
    });
    if (r.ok) {
      try { await api("/api/push/prefs", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ enabled: true }) }); } catch (e) {}
      setNotifyBtn(btn, "on");
      showBanner(false);
      api("/api/push/test", { method: "POST" });
    }
    else alert("No se pudo registrar el dispositivo.");
  } catch (e) {
    alert("Error activando avisos: " + e.message);
  }
}
function setNotifyBtn(btn, state) {
  if (!btn) return;
  btn.classList.remove("on", "off");
  if (state === "on") { btn.textContent = "Avisos ON"; btn.classList.add("on"); }
  else if (state === "off") { btn.textContent = "Avisos OFF"; btn.classList.add("off"); }
  else if (state === "blocked") btn.textContent = "Avisos bloqueado";
  else if (state === "unsupported") btn.textContent = "Avisos n/d";
  else if (state === "error") btn.textContent = "Avisos error";
  else btn.textContent = "Avisos";
}
function startHeartbeat(reg) {
  const send = () => {
    try {
      const t = (reg && reg.active) || navigator.serviceWorker.controller;
      if (t) t.postMessage({ type: "vis", visible: !document.hidden && document.visibilityState === "visible" });
    } catch (e) {}
  };
  const hide = () => {
    try {
      const t = (reg && reg.active) || navigator.serviceWorker.controller;
      if (t) t.postMessage({ type: "vis", visible: false });
    } catch (e) {}
  };
  send();
  setInterval(send, 8000);
  document.addEventListener("visibilitychange", send);
  window.addEventListener("focus", send);
  window.addEventListener("blur", send);
  window.addEventListener("pagehide", hide);
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
      setNotifyBtn(btn, "unsupported");
      showBanner(true, "Este iPhone no expone avisos aqui. Abre el chat desde el icono de la pantalla de inicio.");
      return;
    }
    if (Notification.permission === "denied") {
      setNotifyBtn(btn, "blocked");
      showBanner(true, "Avisos bloqueados. Ve a Ajustes > Notificaciones > Chat y activalos.");
      return;
    }
    // Preferencia persistente (servidor). Decide si los avisos deben estar ON por defecto.
    let pref = true;
    try {
      const pr = await api("/api/push/prefs", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({}) });
      if (pr.ok && pr.data && pr.data.enabled === false) pref = false;
    } catch (e) {}

    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();

    // Helper: crea (si hace falta) y registra la suscripcion en el servidor.
    const ensureRegistered = async () => {
      try {
        const kr = await api("/api/push/key");
        if (!kr.ok || !kr.data || !kr.data.key) { setNotifyBtn(btn, "error"); showBanner(true, "El servidor no tiene push configurado."); return; }
        const cur = await reg.pushManager.getSubscription();
        const s = cur || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToU8(kr.data.key) });
        const rs = await api("/api/push/subscribe", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ subscription: s.toJSON() }) });
        if (rs.ok) { setNotifyBtn(btn, "on"); showBanner(false); }
        else { setNotifyBtn(btn, "error"); showBanner(true, "No se pudieron activar los avisos."); }
      } catch (e) { setNotifyBtn(btn, "error"); showBanner(true, "Error activando avisos: " + e.message); }
    };

    if (sub) {
      // Hay suscripcion local. Si la preferencia es OFF, respetamos OFF.
      if (!pref) { setNotifyBtn(btn, "off"); showBanner(true, "Avisos desactivados. Toca Avisos OFF para volver a activarlos."); return; }
      const st = await api("/api/push/status", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ endpoint: sub.endpoint }) });
      if (st.ok && st.data && st.data.registered) { setNotifyBtn(btn, "on"); showBanner(false); return; }
      // El servidor no la tenia (endpoint renovado por iOS): la volvemos a registrar.
      const rs = await api("/api/push/subscribe", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ subscription: sub.toJSON() }) });
      if (rs.ok) { setNotifyBtn(btn, "on"); showBanner(false); return; }
      setNotifyBtn(btn, "error"); showBanner(true, "No se pudieron verificar los avisos.");
      return;
    }

    // No hay suscripcion local (iOS la solto sola). Fix 4b: si la preferencia es ON y ya
    // hay permiso, la RECREAMOS nosotros en vez de pedir que la reactives a mano.
    if (pref && Notification.permission === "granted") { await ensureRegistered(); return; }
    if (Notification.permission === "granted") { setNotifyBtn(btn, "off"); showBanner(true, "Avisos desactivados. Toca Avisos OFF para volver a activarlos."); return; }
    setNotifyBtn(btn, "plain");
    showBanner(true, "Activa los avisos para que te suene el movil cuando te escriba.");
  } catch (e) { setNotifyBtn(btn, "error"); showBanner(true, "Error consultando avisos: " + e.message); }
}

/* ---------------- depurar ---------------- */
async function debugChat() {
  const ok = confirm("Depurar: se reducira el historial de este chat al minimo y se compactara mi contexto para que vuelva a responder rapido.\n\n¿Seguir?");
  if (!ok) return;
  const r = await api("/api/debug", { method: "POST" });
  if (!r.ok) { toast("No se pudo depurar."); return; }
  if (r.data && r.data.started === false) { toast("Ya hay una depuracion en curso."); }
  else toast("Depurando... compacto mi contexto y luego recargo.");
  let n = 0, seen = false;
  const iv = setInterval(async () => {
    n++;
    const h = await api("/api/history?limit=1");
    const busy = h.data && h.data.busy;
    if (busy) seen = true;
    if ((seen && !busy) || n > 220) {
      clearInterval(iv);
      await loadHistory(); scrollDown(true);
      toast("Listo. Historial depurado y contexto compactado.");
    }
  }, 3000);
}

/* ---------------- wire ---------------- */
window.addEventListener("DOMContentLoaded", async () => {
  $("btnToken").onclick = requestToken;
  $("btnResend").onclick = requestToken;
  $("btnVerify").onclick = verify;
  $("tok").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); verify(); } });
  $("btnLogout").onclick = logout;
  $("btnDebug").onclick = debugChat;
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
  const onNotify = async () => {
    const btn = $("btnNotify");
    const reg = await navigator.serviceWorker.ready.catch(() => null);
    const sub = reg ? await reg.pushManager.getSubscription().catch(() => null) : null;
    const off = btn.classList.contains("off");
    if (sub && !off) await disablePush(btn); else await enablePush(btn);
  };
  $("btnNotify").onclick = onNotify;
  $("btnPushGo").onclick = () => enablePush($("btnNotify"));

  // Pegado al fondo: si el usuario arrastra arriba, paramos de arrastrarle; si vuelve
  // abajo, volvemos a pegarnos. Es la pieza que faltaba (el flag estaba declarado y sin usar).
  const sc = $("scroll");
  if (sc) sc.addEventListener("scroll", () => {
    const gap = sc.scrollHeight - sc.scrollTop - sc.clientHeight;
    if (gap > 140) _stickBottom = false;
    else if (gap < 40) _stickBottom = true;
  }, { passive: true });
  const me = await api("/api/me");
  if (me.ok && me.data && me.data.email) OWNER_EMAIL = String(me.data.email);
  if (me.ok && me.data && me.data.brand) {
    const b = String(me.data.brand);
    const bEl = document.querySelector(".brand"); if (bEl) bEl.textContent = b;
    const tEl = $("typing"); if (tEl) tEl.textContent = b + " esta trabajando...";
    document.title = b;
  }
  if (me.ok && me.data && me.data.ok) showChat(); else showLogin();
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pushState($("btnNotify"));
  });

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.addEventListener("message", (e) => {
      if (e.data && e.data.type === "new-message") tick();
    });
    navigator.serviceWorker.register("/sw.js").then((reg) => { pushState($("btnNotify")); startHeartbeat(reg); }).catch(() => {});
    // Auto-recuperacion: re-verifica los avisos cada 60 s mientras la app esta abierta.
    setInterval(() => { if (!document.hidden) pushState($("btnNotify")); }, 60000);
  }
});
