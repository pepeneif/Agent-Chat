const CACHE = "agent-chat-v3";
const ASSETS = ["/", "/index.html", "/theme.css", "/app.js", "/icon.svg", "/icon-512.png", "/apple-touch-icon.png", "/manifest.webmanifest"];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
// Latido de visibilidad enviado por la pagina. En iOS, al minimizar la PWA la
// pagina se congela y su visibilityState se queda en "visible": por eso no basta
// con leer el estado del cliente. Solo suprimimos la notificacion si la pagina
// reporto estar visible hace muy poco (latido fresco). Si esta congelada, el
// latido caduca y volvemos a notificar.
let lastVisibleAt = 0;
self.addEventListener("message", (e) => {
  const d = e.data || {};
  if (d.type === "vis") lastVisibleAt = d.visible ? Date.now() : 0;
});

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { body: (e.data && e.data.text && e.data.text()) || "" }; }
  const title = d.title || "Agent Chat";
  const body = d.body || "Nuevo mensaje";
  e.waitUntil((async () => {
    // Si ya hay una ventana del chat visible, no notificamos: el usuario esta leyendo.
    try {
      const fresh = (Date.now() - lastVisibleAt) < 16000;
      const ws = await clients.matchAll({ type: "window", includeUncontrolled: true });
      const anyVisible = ws.some((w) => w.visibilityState === "visible" && !w.closed);
      if (fresh && anyVisible) {
        ws.forEach((w) => { if ("postMessage" in w) w.postMessage({ type: "new-message" }); });
        return;   // el usuario esta mirando el chat: sin notificacion
      }
    } catch (_) {}
    await self.registration.showNotification(title, {
      body: body.slice(0, 300),
      icon: "/icon-512.png",
      badge: "/apple-touch-icon.png",
      vibrate: [120, 80, 120],
      tag: "agent-chat-msg",
      renotify: true,
      data: { url: "/" },
    });
  })());
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then((ws) => {
    for (const w of ws) { if ("focus" in w) return w.focus(); }
    if (clients.openWindow) return clients.openWindow("/");
  }));
});
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.pathname.startsWith("/api/")) return; // API siempre a red
  e.respondWith(
    fetch(e.request).then((r) => {
      const copy = r.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
      return r;
    }).catch(() => caches.match(e.request).then((m) => m || caches.match("/index.html")))
  );
});
