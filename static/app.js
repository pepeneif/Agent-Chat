"use strict";
const $ = (id) => document.getElementById(id);
const state = { pending: [], lastAt: 0, timer: null, sending: false, awaitOwn: false, turnControl: false, tmp: [], search: null };
// Marca del despliegue: se sustituye con /api/me (CHAT_BRAND) al arrancar.
let BRAND = "Chat";
// Fix 3 (zoom al escribir) REVERSIBLE: para volver al comportamiento anterior, cambia a false.
const FIX_COMPOSER_ZOOM = true;
const FIX_ZOOM = FIX_COMPOSER_ZOOM;   // alias
if (FIX_COMPOSER_ZOOM) document.documentElement.classList.add("fix-zoom");
else document.documentElement.classList.add("no-fix-zoom");

async function api(path, opts) {
  // Timeout: si el movil pierde la red, la peticion no puede quedar colgada para siempre
  // (antes el compositor se quedaba lleno indefinidamente).
  const o = Object.assign({ credentials: "same-origin" }, opts || {});
  if (!o.signal && typeof AbortController !== "undefined") {
    const ctrl = new AbortController();
    o.signal = ctrl.signal;
    setTimeout(() => ctrl.abort(), 25000);
  }
  let r;
  try {
    r = await fetch(path, o);
  } catch (e) {
    return { ok: false, status: 0, data: null, error: String(e && e.message || e) };
  }
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
  const r = await api("/api/request-token", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
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
function _splitRow(line) {
  let t = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return t.split("|").map(function (x) { return x.trim(); });
}
function _isAlignRow(line) {
  if (line.indexOf("-") < 0) return false;
  var cells = _splitRow(line);
  return cells.length > 0 && cells.every(function (c) { return /^:?-{2,}:?$/.test(c.replace(/\s+/g, "")); });
}
function flushTable(rows, alignRow) {
  if (!rows.length) return "";
  var aligns = (alignRow || rows[0]).map(function (c) {
    var t = c.replace(/\s+/g, "");
    return t.charAt(0) === ":" && t.charAt(t.length - 1) === ":" ? "center"
         : t.charAt(t.length - 1) === ":" ? "right" : "";
  });
  var head = "<thead><tr>" + rows[0].map(function (c, i) {
    return "<th" + (aligns[i] ? ' style="text-align:' + aligns[i] + '"' : "") + ">" + mdInline(c) + "</th>";
  }).join("") + "</tr></thead>";
  var body = "";
  if (rows.length > 1) {
    body = "<tbody>" + rows.slice(1).map(function (r) {
      // normalizar al ancho de la cabecera
      var cells = r.slice(0, rows[0].length);
      while (cells.length < rows[0].length) cells.push("");
      return "<tr>" + cells.map(function (c, i) {
        return "<td" + (aligns[i] ? ' style="text-align:' + aligns[i] + '"' : "") + ">" + mdInline(c) + "</td>";
      }).join("") + "</tr>";
    }).join("") + "</tbody>";
  }
  return '<div class="tblwrap"><table class="md">' + head + body + "</table></div>";
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
    if (/^\s*\|/.test(line) && i + 1 < lines.length && /^\s*\|/.test(lines[i + 1]) && _isAlignRow(lines[i + 1])) {
      const rows = [_splitRow(line)];
      const alignRow = _splitRow(lines[i + 1]);
      i += 2;                                  // cabecera + fila separadora
      while (i < lines.length && /^\s*\|/.test(lines[i])) { rows.push(_splitRow(lines[i])); i++; }
      out.push(flushTable(rows, alignRow));
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
           && !/^\s*>/.test(lines[i]) && !/^(#{1,6})\s+/.test(lines[i]) && !/^\s*(---+|\*\*\*+)\s*$/.test(lines[i]) && !/^\s*\|/.test(lines[i])) {
      para.push(lines[i]); i++;
    }
    out.push("<p>" + mdInline(para.join("\n")).replace(/\n/g, "<br>") + "</p>");
  }
  let html = out.join("\n");
  return html;
}
function render(m) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + (m.role === "user" ? "user" : m.role === "system" ? "system" : "bot");
  wrap.dataset.id = m.id || "";
  if (m.role === "bot" && m.live) wrap.classList.add("live");
  const who = m.role === "user" ? "Tu" : m.role === "bot" ? BRAND : "";
  let inner = "";
  if (m.role !== "system") inner += `<div class="who">${who} · ${fmtTime(m.at)}</div>`;
  // Adjuntos: las URLs /api/file/<hex> no se pintan como texto ni como enlace "abrir"
  // (no habia forma de cerrar el visor). Las IMAGENES se muestran inline; el resto se
  // ofrece como boton "Descargar" (el backend las sirve con Content-Disposition attachment,
  // asi que bajan a Archivos del telefono). Peticion de Jose, 2026-10-19.
  const ids = (m.content.match(/\/api\/file\/[0-9a-f]{32}/g) || []);
  let text = m.content;
  ids.forEach((u) => { text = text.replace(u, ""); });
  let body = linkify(text);
  wrap.innerHTML = `<div><div>${inner}</div><div class="bubble">${body}</div></div>`;
  const bubble = wrap.querySelector(".bubble");
  ids.forEach((u) => {
    const img = new Image();
    img.src = u; img.alt = "adjunto";
    img.onload = () => { bubble.appendChild(img); if (_stickBottom) { scrollDown(); } };
    img.onerror = () => {
      if (bubble.querySelector('a.file[href="' + u + '"]')) return;
      const a = document.createElement("a");
      a.className = "file"; a.href = u;
      a.setAttribute("download", ""); a.textContent = "Descargar";
      bubble.appendChild(a); if (_stickBottom) { scrollDown(); }
    };
  });
  return wrap;
}
function jumpEl() { return $("btnJump"); }
function _updJump() {
  const b = jumpEl(), s = $("scroll");
  if (!b || !s) return;
  const up = !atBottom(s, 40);     // hay contenido por debajo del borde inferior
  const vis = up || !$("typing").classList.contains("hidden");
  b.classList.toggle("hidden", !vis);
}
function jumpToEnd() {
  _stickBottom = true;
  const s = $("scroll");
  if (s) { _forceBottom(s); pinBottom(); }
  const msgs = document.querySelectorAll(".msg");
  if (msgs.length) revealMessage(msgs[msgs.length - 1]);
  _updJump();
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
function _tmpFp(text) { return (text || "").replace(/\s+/g, " ").trim(); }
function newTmpId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    try { return "tmp-" + crypto.randomUUID(); } catch (e) {}
  }
  return "tmp-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
}
function addTmp(text, at) {
  const id = newTmpId();
  const el = upsert({ id: id, role: "user", content: text, at: at || Date.now() });
  el.classList.add("tmp");
  state.tmp.push({ id: id, text: _tmpFp(text) });
  return el;
}
function dropTmp(el) {
  if (!el) return;
  const id = el.dataset.id || "";
  state.tmp = state.tmp.filter((r) => r.id !== id);
  const node = (id && $("scroll").querySelector(`[data-id="${CSS.escape(id)}"]`)) || null;
  if (node) node.remove();
}
function upsert(m) {
  let existing = (m.id && $("scroll").querySelector(`[data-id="${CSS.escape(m.id)}"]`)) || null;
  if (!existing && m.role === "user") {
    const fp = _tmpFp(m.content);
    // Con adjuntos el servidor concatena "texto\n[adjunto] ...": la huella no es igual,
    // asi que tambien emparejamos si el mensaje empieza por la huella de la burbuja.
    const idx = state.tmp.findIndex((r) => r.text === fp || (r.text && fp.startsWith(r.text)));

    if (idx >= 0) {
      const row = state.tmp[idx];
      state.tmp.splice(idx, 1);
      existing = (row.id && $("scroll").querySelector(`[data-id="${CSS.escape(row.id)}"]`)) || null;
      if (existing) existing.dataset.id = m.id || "";
    }
  }
  if (existing) {
    const fresh = render(m);
    existing.className = fresh.className;
    const freshBubble = fresh.querySelector(".bubble");
    const curBubble = existing.querySelector(".bubble");
    if (freshBubble && curBubble) curBubble.innerHTML = freshBubble.innerHTML;
    const freshWho = fresh.querySelector(".who");
    const curWho = existing.querySelector(".who");
    if (freshWho && curWho) curWho.innerHTML = freshWho.innerHTML;
    existing.dataset.id = m.id || existing.dataset.id;
    existing.dataset.at = String(m.at || 0);
    return existing;
  }
  const el = render(m);
  el.dataset.at = String(m.at || 0);
  $("scroll").appendChild(el);
  return el;
}

async function loadHistory() {
  const r = await api("/api/history?limit=80");
  if (r.status === 401) { showLogin(); return; }
  const s = $("scroll"); s.innerHTML = "";
  (r.data.messages || []).forEach((m) => { upsert(m); if (m.at > state.lastAt) state.lastAt = m.at; });
  _stickBottom = true;               // una carga limpia siempre arranca pegado al ultimo mensaje
  scrollDown(true);
  pinBottom();                       // y lo reafirmamos tras el layout (imagenes/markdown)
  setTimeout(() => { const s2 = $("scroll"); if (s2 && _stickBottom) _forceBottom(s2); _updJump(); }, 300);
  _updJump();
  if (state.search && state.search.active && !hasTextSelection()) applySearchMarks();
}

// Carga hacia ATRAS (mensajes mas antiguos) y los PREPONE arriba, conservando la
// posicion visual. Sirve al buscador para alcanzar coincidencias fuera de la ventana
// de 80 mensajes ya cargada, sin romper el orden cronologico del DOM.
async function loadOlder(limit) {
  const s = $("scroll");
  if (!s) return 0;
  const first = s.querySelector(".msg[data-at]");
  const before = first ? Number(first.dataset.at) : (state.lastAt || Date.now());
  if (!before) return 0;
  const r = await api("/api/history?limit=" + (limit || 120) + "&before=" + encodeURIComponent(before));
  if (!r.ok || !r.data || !(r.data.messages || []).length) return 0;
  const prevH = s.scrollHeight, prevTop = s.scrollTop;
  const frag = document.createDocumentFragment();
  let n = 0;
  r.data.messages.forEach((m) => {
    if (m.id && s.querySelector('[data-id="' + CSS.escape(m.id) + '"]')) return;
    const el = render(m);
    el.dataset.at = String(m.at || 0);
    frag.appendChild(el); n++;
  });
  if (n) {
    s.insertBefore(frag, s.firstChild);
    s.scrollTop = prevTop + (s.scrollHeight - prevH);   // mantener lo que se veia
  }
  return n;
}

/* ---------------- polling ---------------- */
let _failStreak = 0;   // polls fallidos seguidos (red / cancelacion HTTP/3 en movil)
let _tickBusy = false; // evita apilar polls: si la red se atasca, no se encolan decenas
async function tick() {
  if (_tickBusy) return;              // ya hay un poll en vuelo; no encolar mas
  _tickBusy = true;
  try {
    await _tickOnce();
  } finally {
    _tickBusy = false;
  }
}
async function _tickOnce() {
  reportView(false);
  // Poll incremental: pedir SOLO lo nuevo (after=lastAt). Antes bajaba 40 mensajes
  // completos (~65 KB) cada 700 ms; en el movil eso saturaba el enlace y HTTP/3
  // cancelaba la respuesta (chat "congelado"). El primer poll (lastAt=0) trae el lote.
  const url = state.lastAt > 0
    ? "/api/history?limit=40&after=" + encodeURIComponent(state.lastAt)
    : "/api/history?limit=40";
  const r = await api(url);
  if (r.status === 401) { stopPolling(); showLogin(); return; }
  if (!r.ok || !r.data) {
    // El poll se cayo (p. ej. HTTP/3 cancela la respuesta en el movil). Antes esto
    // dejaba el chat "congelado" hasta cerrar y reabrir la PWA; ahora se auto-recupera
    // recargando el historial en cuanto se acumulan fallos seguidos.
    if (++_failStreak >= 4) { _failStreak = 0; try { await loadHistory(); } catch (e) {} }
    return;
  }
  _failStreak = 0;
  if (r.data && typeof r.data.busy === "boolean") setTyping(r.data.busy, r.data.busySince, r.data.waitingBots);
  const msgs = (r.data.messages || []).filter((m) => m.at > state.lastAt);
  const hadBot = msgs.some((m) => m.role === "bot" || m.role === "system");
  const near = atBottom($("scroll"), 120);  // medir ANTES de insertar
  if (near) _stickBottom = true;            // el usuario estaba abajo -> seguimos pegados
  const stick = _stickBottom;               // fuente de verdad: el flag, no un unico umbral
  msgs.sort((a, b) => a.at - b.at);
  const els = msgs.map((m) => {
    const el = upsert(m);
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
  _updJump();
  if (state.search && state.search.active && msgs.length && !hasTextSelection()) applySearchMarks();
}
function startPolling() { stopPolling(); state.timer = setInterval(tick, 700); tick(); }
function stopPolling() { if (state.timer) clearInterval(state.timer); state.timer = null; }
let _lastViewKey = "";
let _lastViewAt = 0;
function _viewSnapshot() {
  return { visible: !document.hidden, atBottom: atBottom($("scroll"), 120) };
}
function _sendView(snapshot) {
  return fetch("/api/view", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(snapshot),
    credentials: "same-origin",
    keepalive: true,
  });
}
// Reporta POST /api/view solo al cambiar el estado o cada ~5 s (nunca en cada tick).
async function reportView(force) {
  const vs = _viewSnapshot();
  const key = (vs.visible ? "1" : "0") + ":" + (vs.atBottom ? "1" : "0");
  const now = Date.now();
  if (!force && key === _lastViewKey && (now - _lastViewAt) < 5000) return;
  _lastViewKey = key;
  _lastViewAt = now;
  try { await _sendView(vs); } catch (e) {}
}
function reportVisibleFalse() {
  _sendView({ visible: false, atBottom: atBottom($("scroll"), 120) }).catch(() => {});
}
let _typingAnchor = 0;
let _waitingAnchor = 0;
// H10: nucleo puro del contador. Devuelve los segundos del turno en curso; sin marca
// fiable de inicio ancla al primer parametro nowMs (primer poll con busy) y conserva el
// ancla entre llamadas. El ancla externa (_typingAnchor) se resetea al quitar busy.
function typingSeconds(nowMs, sinceMs) {
  const stamp = Number(sinceMs) || 0;
  if (stamp) { _typingAnchor = stamp; return Math.max(0, Math.round((nowMs - stamp) / 1000)); }
  if (!_typingAnchor) _typingAnchor = nowMs;
  return Math.max(0, Math.round((nowMs - _typingAnchor) / 1000));
}
function setTyping(on, sinceMs, waitingBots) {
  const el = $("typing");
  const tx = $("typingText") || el;
  if (!on) {
    _typingAnchor = 0;
    _waitingAnchor = 0;
    el.classList.add("hidden");
    _updJump();
    return;
  }
  let text;
  if (waitingBots && waitingBots.since) {
    // espera de delegaciones: ancla PROPIA que se resetea por cada ventana (since)
    _waitingAnchor = Number(waitingBots.since) || _waitingAnchor || Date.now();
    const waitSecs = Math.max(0, Math.round((Date.now() - _waitingAnchor) / 1000));
    const peers = (waitingBots.peers && waitingBots.peers.length)
      ? " " + waitingBots.peers.join(", ")
      : "";
    text = BRAND + ", está esperando a" + peers +
           " (" + waitSecs + "s)";
  } else {
    _waitingAnchor = 0;
    // H10: si busy=true y el servidor no da busySince, anclamos al primer poll con busy
    // y seguimos contando cada tick. Un sinceMs valido (marca real del turno) reancla.
    const secs = typingSeconds(Date.now(), sinceMs);
    text = secs > 3
      ? BRAND + " esta trabajando... (" + secs + "s)"
      : BRAND + " esta trabajando...";
  }
  tx.textContent = text;
  el.classList.remove("hidden");
  _updJump();
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
  // Eco optimista INMEDIATO: la burbuja aparece con la hora local correcta (antes at:0
  // mostraba "1 ene 07:30" en Asia/Singapore hasta que llegaba el mensaje real).
  const tmpEl = t.trim() ? addTmp(t, Date.now()) : null;
  if (tmpEl && _stickBottom) scrollDown(true);
  // Limpiamos el campo YA, sin esperar al servidor: en movil el ida-y-vuelta tardaba
  // segundos y parecia que el chat se habia caido.
  $("text").value = ""; autosize();
  state.pending = []; $("attachrow").innerHTML = "";
  state.awaitOwn = true;       // al llegar, mi mensaje queda pegado abajo
  state.sending = false;       // F3: el compositor NUNCA se bloquea
  $("btnSend").disabled = false;
  const r = await api("/api/send", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ text: t, attachments: atts })
  });
  if (!r.ok) {
    // Fallo real de red/servidor: devolvemos el texto al campo y quitamos la burbuja.
    dropTmp(tmpEl);
    $("text").value = t; autosize();
    alert("No se pudo enviar.");
    return;
  }
  setTyping(true);
  setTimeout(tick, 300);
}


async function stopTurn() {
  if (!state.turnControl) return;
  const b = $("btnStop"); if (b) { b.disabled = true; }
  try {
    const r = await api("/api/interrupt", { method: "POST" });
    if (r.status === 502) { toast("No se pudo detener el turno."); }
    else toast("Deteniendo el turno...");
    setTimeout(tick, 300);
  } catch (e) {
    toast("Error deteniendo el turno.");
  } finally {
    if (b) b.disabled = false;
  }
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
// El antiguo boton "Avisos" pasa a ser el PUNTO de la cabecera: verde = avisos
// activos, rojo = no. Sigue llamandose setNotifyBtn para no tocar los llamadores.
function setNotifyBtn(dot, state) {
  if (!dot) return;
  dot.classList.remove("on", "off");
  const label = {
    "on": "Avisos activos (toca para desactivar)",
    "off": "Avisos desactivados (toca para activar)",
    "blocked": "Avisos bloqueados en el sistema",
    "unsupported": "Avisos no disponibles en este navegador",
    "error": "Error consultando los avisos",
    "plain": "Activa los avisos",
  }[state] || "Avisos";
  if (state === "on") dot.classList.add("on"); else dot.classList.add("off");
  dot.title = label;
  dot.setAttribute("aria-label", label);
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

/* ---------------- busqueda (2026-10-15) ---------------- */
// Busca en el historial del chat (lo no depurado) y, opcionalmente, en la wiki de
// memoria del bot. El marcado se hace sobre los nodos de texto ya renderizados, asi
// que respeta el formato (negritas, codigo, tablas) sin re-parsear nada.
const SEARCH_MIN = 2;
let _searchTimer = null;

// Hay una seleccion de texto viva dentro del chat o del panel de wiki? Si la hay,
// NO reescribimos los <mark> (applySearchMarks destruye la seleccion en curso): se
// pospone el repintado para no arrancarle a Jose lo que acaba de seleccionar.
function hasTextSelection() {
  try {
    const sel = (window.getSelection && window.getSelection()) || null;
    if (!sel || sel.isCollapsed || !sel.rangeCount) return false;
    const node = sel.anchorNode;
    if (!node) return false;
    const el = node.nodeType === 1 ? node : node.parentElement;
    if (!el) return false;
    const sc = $("scroll"), wp = $("searchWikiHits");
    return (sc && sc.contains(el)) || (wp && wp.contains(el));
  } catch (e) { return false; }
}

function foldStr(str) {
  let out = "";
  for (const ch of String(str || "")) {
    let d;
    try { d = ch.normalize("NFD").replace(/[\u0300-\u036f]/g, ""); } catch (e) { d = ch; }
    let f;
    if (d.length === 1) f = d.toLowerCase();
    else f = ch.length === 1 ? ch.toLowerCase() : ch;
    if (f.length !== 1) f = ch;   // conservar longitud 1:1 (alineacion de indices)
    out += f;
  }
  return out;
}
function occIn(hay, needle) {
  const out = [];
  if (!needle) return out;
  let i = 0;
  while (true) {
    const k = hay.indexOf(needle, i);
    if (k < 0) break;
    out.push(k);
    i = k + needle.length;
  }
  return out;
}
function clearSearchMarks() {
  // Solo quitamos NUESTRAS marcas (data-smark): un <mark> que venga del markdown del
  // mensaje no es nuestro y no se debe destruir (H3 auditoria 2026-10-15).
  document.querySelectorAll(".msg .bubble mark[data-smark]").forEach((mk) => {
    const t = document.createTextNode(mk.textContent);
    mk.parentNode.replaceChild(t, mk);
  });
  document.querySelectorAll(".msg[data-hit]").forEach((m) => m.removeAttribute("data-hit"));
}
function bubbleTextNodes(el) {
  const nodes = [];
  const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
  let n;
  while ((n = w.nextNode())) nodes.push(n);
  return nodes;
}
// Marca las ocurrencias de la consulta en los mensajes ya cargados. Devuelve la lista
// plana de <mark> en orden de aparicion.
function applySearchMarks() {
  const t = state.search;
  const nq0 = (t && t.active && t.q) ? foldStr(t.q) : "";
  if (!nq0) { if (!hasTextSelection()) clearSearchMarks(); return []; }
  // Si hay una seleccion viva Y las marcas ya corresponden a ESTA consulta, no repintamos
  // (no arrancarle a Jose la seleccion). Pero si la consulta CAMBIO, si repintamos: dejar
  // la marca del termino viejo seria peor (marca y contador desincronizados).
  if (hasTextSelection() && t._appliedQ === t.q && (t._marks || []).length) return t._marks;
  clearSearchMarks();
  const nq = nq0;
  const ids = new Set((t.hits || []).map((h) => h.id));
  const marks = [];
  document.querySelectorAll(".msg[data-id]").forEach((msg) => {
    if (!ids.has(msg.dataset.id)) return;
    const bubble = msg.querySelector(".bubble");
    if (!bubble) return;
    msg.setAttribute("data-hit", "1");
    bubbleTextNodes(bubble).forEach((node) => {
      const txt = node.nodeValue || "";
      const folded = foldStr(txt);
      const occ = occIn(folded, nq);
      if (!occ.length) return;
      const frag = document.createDocumentFragment();
      let last = 0;
      occ.forEach((pos) => {
        if (pos > last) frag.appendChild(document.createTextNode(txt.slice(last, pos)));
        const mk = document.createElement("mark");
        mk.setAttribute("data-smark", "1");
        mk.textContent = txt.slice(pos, pos + nq.length);
        frag.appendChild(mk); marks.push(mk);
        last = pos + nq.length;
      });
      if (last < txt.length) frag.appendChild(document.createTextNode(txt.slice(last)));
      node.parentNode.replaceChild(frag, node);
    });
  });
  t._marks = marks;
  t._appliedQ = t.q;
  return marks;
}
function searchTotal() {
  const t = state.search; if (!t) return 0;
  return (t.hits || []).reduce((a, h) => a + (h.count || 0), 0);
}
function setSearchCount() {
  const t = state.search; if (!t) return;
  const el = $("searchCount"); if (!el) return;
  if (!t.q || !t.active) { el.innerHTML = ""; return; }
  // Dos renglones para dejar el maximo ancho al campo: arriba las ocurrencias en los
  // mensajes (posicion actual de total), abajo las de la wiki (solo si se busca ahi).
  const total = searchTotal();
  const wc = wikiCount();
  const showWiki = !!(t.wiki && t.wiki.length);
  const msgLine = total ? ((t.i + 1) + " de " + total)
                        : (showWiki ? "0 mensajes" : "sin result.");
  let h = "<span class=\"cl ms\">" + esc(msgLine) + "</span>";
  if (showWiki) h += "<span class=\"cl wk\">wiki " + esc(String(wc)) + "</span>";
  el.innerHTML = h;
}
function wikiCount() {
  const t = state.search;
  return (t.wiki || []).reduce((a, h) => a + (h.count || 0), 0);
}
function renderWikiHits() {
  const box = $("searchWikiHits"); if (!box) return;
  const t = state.search;
  const hits = (t && t.wiki) || [];
  if (!hits.length) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  let h = "";
  hits.slice(0, 12).forEach((f) => {
    const line = (f.hits && f.hits[0]) ? f.hits[0].text : "";
    h += "<div class=\"wh\"><b>" + esc(f.path) + "</b> (" + f.count + ")" +
         (line ? "<br>" + esc(line) : "") + "</div>";
  });
  box.innerHTML = h;
  box.classList.remove("hidden");
}
// Asegura que el mensaje `id` este en el DOM (cargando hacia atras si hace falta).
async function ensureMessage(id) {
  const s = $("scroll");
  if (!s) return false;
  if (s.querySelector('[data-id="' + CSS.escape(id) + '"]')) return true;
  for (let k = 0; k < 40; k++) {           // tope: no encadenar cargas sin fin
    const n = await loadOlder(150);
    if (!n) return false;
    if (s.querySelector('[data-id="' + CSS.escape(id) + '"]')) return true;
  }
  return false;
}
async function gotoCurrent() {
  const t = state.search;
  if (!t || !t.active || !t.hits || !t.hits.length) { applySearchMarks(); setSearchCount(); return; }
  t.i = Math.max(0, Math.min(t.i, t.hits.length - 1));
  const hit = t.hits[t.i];
  t.occ = Math.max(0, Math.min(t.occ || 0, (hit.count || 1) - 1));
  await ensureMessage(hit.id);
  const marks = applySearchMarks();
  // Indice plano de la ocurrencia buscada = ocurrencias de todos los mensajes previos + occ
  let flat = t.occ;
  for (let k = 0; k < t.i; k++) flat += (t.hits[k].count || 0);
  marks.forEach((mk, i) => mk.classList.toggle("cur", i === flat));
  const cur = marks[flat];
  const el = cur ? cur.closest(".msg") : null;
  if (el) { _stickBottom = false; revealMessage(el); }
  else if (hit.id) {
    const msg = $("scroll").querySelector('[data-id="' + CSS.escape(hit.id) + '"]');
    if (msg) { _stickBottom = false; revealMessage(msg); }
  }
  setSearchCount();
}
function searchStep(delta) {
  const t = state.search;
  if (!t || !t.hits || !t.hits.length) return;
  const total = searchTotal();
  let flat = 0;
  for (let k = 0; k < t.i; k++) flat += (t.hits[k].count || 0);
  flat += (t.occ || 0) + delta;
  if (flat < 0) flat = total - 1;
  if (flat >= total) flat = 0;
  // traducir flat -> (i, occ)
  let acc = 0;
  for (let k = 0; k < t.hits.length; k++) {
    const c = t.hits[k].count || 0;
    if (flat < acc + c) { t.i = k; t.occ = flat - acc; break; }
    acc += c;
  }
  gotoCurrent();
}
async function runSearch(q) {
  const t = state.search = state.search || { active: false, hits: [], wiki: [], i: 0, occ: 0 };
  t.q = q;
  if (!q || q.length < SEARCH_MIN) {
    t.active = false; t.hits = []; t.wiki = []; t.i = 0; t.occ = 0;
    clearSearchMarks(); renderWikiHits(); setSearchCount();
    return;
  }
  t.active = true;
  let url = "/api/search?q=" + encodeURIComponent(q);
  if ($("searchWiki") && $("searchWiki").checked) url += "&wiki=1";
  const r = await api(url);
  if (!r.ok || !r.data) { t.hits = []; t.wiki = []; clearSearchMarks(); setSearchCount(); return; }
  if (r.data.q !== t.q) return;             // respuesta obsoleta
  t.hits = r.data.chat || [];
  t.wiki = r.data.wiki || [];
  t.i = 0; t.occ = 0;
  renderWikiHits();
  await gotoCurrent();
}
function openSearch() {
  const p = $("searchPanel");
  if (!p) return;
  p.classList.remove("hidden");
  const b = $("btnSearch"); if (b) b.setAttribute("aria-expanded", "true");
  const inp = $("searchInput");
  if (inp) { inp.focus(); inp.select(); }
  try { p.scrollIntoView({ block: "nearest" }); } catch (e) {}
}
function closeSearch() {
  const p = $("searchPanel");
  if (p) p.classList.add("hidden");
  const b = $("btnSearch"); if (b) b.setAttribute("aria-expanded", "false");
  if (state.search) { state.search.active = false; }
  clearSearchMarks(); renderWikiHits(); setSearchCount();
}
function toggleSearch() {
  const p = $("searchPanel");
  if (!p) return;
  if (p.classList.contains("hidden")) openSearch(); else closeSearch();
}
function wireSearch() {
  const b = $("btnSearch"); if (b) b.onclick = toggleSearch;
  const inp = $("searchInput");
  if (inp) {
    inp.addEventListener("input", () => {
      const q = inp.value.trim();
      if (_searchTimer) clearTimeout(_searchTimer);
      if (q.length < SEARCH_MIN) { runSearch(q); return; }
      _searchTimer = setTimeout(() => runSearch(q), 220);
    });
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); searchStep(e.shiftKey ? -1 : 1); }
      else if (e.key === "Escape") { e.preventDefault(); closeSearch(); }
    });
  }
  const pv = $("btnPrev"); if (pv) pv.onclick = () => searchStep(-1);
  const nx = $("btnNext"); if (nx) nx.onclick = () => searchStep(1);
  const wk = $("searchWiki");
  if (wk) wk.addEventListener("change", () => { const q = ($("searchInput") || {}).value; if (q && q.trim().length >= SEARCH_MIN) runSearch(q.trim()); });
}

/* ---------------- wire ---------------- */
window.addEventListener("DOMContentLoaded", async () => {
  $("btnToken").onclick = requestToken;
  $("btnResend").onclick = requestToken;
  $("btnVerify").onclick = verify;
  $("tok").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); verify(); } });
  $("btnLogout").onclick = logout;
  $("btnDebug").onclick = debugChat;
  wireSearch();
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
  if (state.turnControl) {
    const top = $("topBar");
    if (top) {
      const btn = document.createElement("button");
      btn.className = "ghost stop";
      btn.id = "btnStop";
      btn.title = "Detener el turno";
      btn.textContent = "Detener";
      btn.onclick = stopTurn;
      top.insertBefore(btn, $("btnSearch"));
    }
  }
  const onNotify = async () => {
    const btn = $("statusDot");
    const reg = await navigator.serviceWorker.ready.catch(() => null);
    const sub = reg ? await reg.pushManager.getSubscription().catch(() => null) : null;
    const off = btn.classList.contains("off");
    if (sub && !off) await disablePush(btn); else await enablePush(btn);
  };
  const dotEl = $("statusDot");
  if (dotEl) {
    dotEl.onclick = onNotify;
    dotEl.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onNotify(); } });
  }
  $("btnPushGo").onclick = () => enablePush($("statusDot"));

  // Pegado al fondo: si el usuario arrastra arriba, paramos de arrastrarle; si vuelve
  // abajo, volvemos a pegarnos. Es la pieza que faltaba (el flag estaba declarado y sin usar).
  const sc = $("scroll");
  if (sc) sc.addEventListener("scroll", () => {
    const gap = sc.scrollHeight - sc.scrollTop - sc.clientHeight;
    if (gap > 140) _stickBottom = false;
    else if (gap < 40) _stickBottom = true;
    _updJump();
  }, { passive: true });
  const jb = $("btnJump"); if (jb) jb.onclick = jumpToEnd;
  const me = await api("/api/me");
  if (me.ok && me.data && me.data.turnControl) state.turnControl = true;
  // NV2: sin sesion el servidor ya no devuelve el correo del dueno; el cliente
  // no lo necesita para nada (el servidor decide a quien manda el codigo).
  if (me.ok && me.data && me.data.brand) {
    const b = String(me.data.brand);
    BRAND = b;
    const bEl = document.querySelector(".brand"); if (bEl) bEl.textContent = b;
    const txEl = $("typingText"); if (txEl) txEl.textContent = b + " esta trabajando...";
    document.title = b;
  }
  if (me.ok && me.data && me.data.ok) showChat(); else showLogin();
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) reportVisibleFalse();
    else reportView(true);
    if (!document.hidden) pushState($("statusDot"));
  });
  window.addEventListener("pagehide", () => { reportVisibleFalse(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("searchPanel") && !$("searchPanel").classList.contains("hidden")) closeSearch(); });

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.addEventListener("message", (e) => {
      if (e.data && e.data.type === "new-message") tick();
    });
    navigator.serviceWorker.register("/sw.js").then((reg) => { pushState($("statusDot")); startHeartbeat(reg); }).catch(() => {});
    // Auto-recuperacion: re-verifica los avisos cada 60 s mientras la app esta abierta.
    setInterval(() => { if (!document.hidden) pushState($("statusDot")); }, 60000);
  }
});
