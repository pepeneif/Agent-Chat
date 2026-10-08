#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent-Chat — minichat privado (PWA) entre una persona y un agente/bot.

Un solo fichero Python (stdlib). Autenticacion por token de un solo uso enviado por
email. Dispara turnos en el hilo dedicado del agente en OpenMausBot y publica sus
respuestas, con acuse rapido + respuesta final, y notificaciones Web Push (VAPID).

Toda la configuracion se toma de variables de entorno (ver .env.example).
"""
import os, re, json, time, uuid, sqlite3, secrets, smtplib, threading, mimetypes, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote
from urllib.request import urlopen, Request
from email.message import EmailMessage
from email.utils import make_msgid, formatdate
from email.headerregistry import Address
try:
    from pywebpush import webpush, WebPushException
    _HAS_PUSH = True
except Exception:
    _HAS_PUSH = False

# ---------- config ----------
def _env(name, default=""):
    return os.environ.get(name, default)

DATA_DIR   = _env("CHAT_DATA", os.path.expanduser("~/chat/data"))
STATIC_DIR = _env("CHAT_STATIC", os.path.expanduser("~/chat/static"))
PORT       = int(_env("CHAT_PORT", "8792"))

# OpenMausBot local (el agente corre en el mismo host)
OMB        = _env("OMB_URL", "http://127.0.0.1:8799")
BOT_ID     = _env("OMB_BOT_ID")       # id del bot/agente que responde
THREAD_ID  = _env("OMB_THREAD_ID")    # hilo dedicado donde vive la conversacion

# Quien puede entrar (un solo email)
OWNER      = _env("CHAT_OWNER", "").lower()

# Correo saliente del codigo de acceso (SMTP directo al MX del dominio emisor)
MAIL_FROM_H = _env("MAIL_FROM_NAME", "Agent Chat")
MAIL_FROM_A = _env("MAIL_FROM_ADDR", "no-reply@example.com")
MAIL_FROM_D = _env("MAIL_FROM_DOMAIN", "example.com")
SMTP_HOST  = _env("SMTP_HOST", "127.0.0.1")
SMTP_PORT  = int(_env("SMTP_PORT", "25"))

VAPID_SUB  = _env("VAPID_SUB", "mailto:no-reply@example.com")

SESSION_DAYS = 30
TOKEN_MIN    = 15
MAX_UPLOAD   = 25 * 1024 * 1024

os.makedirs(DATA_DIR, exist_ok=True)
FILES_DIR = os.path.join(DATA_DIR, "files")
os.makedirs(FILES_DIR, exist_ok=True)
DB = os.path.join(DATA_DIR, "chat.db")
VAPID_FILE = os.path.join(DATA_DIR, "vapid.json")
VAPID = {}
try:
    with open(VAPID_FILE) as _f:
        VAPID = json.load(_f)
except Exception:
    VAPID = {}
_dblock = threading.Lock()
_dispatch_lock = threading.Lock()   # serializa los turnos: un dispatch a la vez
_busy = {"on": False, "since": 0, "q": 0}   # hay un turno en curso (para avisar al cliente)
_mirror = {"on": False}   # hay un hilo "espejo" siguiendo el hilo del agente

# ---------- db ----------
def db():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY, role TEXT, content TEXT, at INTEGER, uid TEXT);
        CREATE TABLE IF NOT EXISTS logins (
            token TEXT PRIMARY KEY, created INTEGER, expires INTEGER, used INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessions (
            sid TEXT PRIMARY KEY, created INTEGER, expires INTEGER);
        CREATE TABLE IF NOT EXISTS files (
            id TEXT PRIMARY KEY, name TEXT, mime TEXT, size INTEGER, created INTEGER);
        CREATE TABLE IF NOT EXISTS rl (k TEXT PRIMARY KEY, n INTEGER, ts INTEGER);
        CREATE TABLE IF NOT EXISTS push_subs (endpoint TEXT PRIMARY KEY, sub TEXT, created INTEGER);
        """)

def now_ms(): return int(time.time() * 1000)

def add_msg(role, content, uid=None):
    mid = uuid.uuid4().hex
    with _dblock, db() as c:
        c.execute("INSERT INTO messages(id,role,content,at,uid) VALUES(?,?,?,?,?)",
                  (mid, role, content, now_ms(), uid))
    return {"id": mid, "role": role, "content": content, "at": now_ms()}

def already_sent(role, content):
    """True si ese mismo texto ya esta guardado (evita publicar la respuesta por duplicado)."""
    if not content:
        return False
    try:
        with db() as c:
            row = c.execute(
                "SELECT 1 FROM messages WHERE role=? AND content=? LIMIT 1",
                (role, content)).fetchone()
        return bool(row)
    except Exception:
        return False

def history(limit=60, before=None):
    with db() as c:
        if before:
            rows = c.execute("SELECT * FROM messages WHERE at<? ORDER BY at DESC LIMIT ?",
                             (int(before), limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM messages ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
    return list(reversed([dict(r) for r in rows]))

# ---------- email ----------
def send_token_email(to_addr, token):
    m = EmailMessage()
    m["From"] = Address(MAIL_FROM_H, MAIL_FROM_A.split("@")[0], MAIL_FROM_D)
    m["To"] = to_addr
    m["Subject"] = "Tu codigo de acceso al chat"
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain=MAIL_FROM_D)
    body = (
        "Este es tu codigo de acceso de un solo uso para el chat.\n\n"
        f"    {token}\n\n"
        f"Caduca en {TOKEN_MIN} minutos. Pegalo en la pantalla del chat.\n"
        "Si no lo has pedido tu, ignora este correo.\n"
    )
    m.set_content(body, charset="utf-8")
    raw = m.as_string()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=25) as s:
        s.ehlo(MAIL_FROM_D)
        c1, r1 = s.mail(MAIL_FROM_A)
        c2, r2 = s.rcpt(to_addr)
        if c2 >= 400:
            raise RuntimeError(f"rcpt rejected: {c2} {r2!r}")
        c3, r3 = s.data(raw)
        log_line(f"MAIL to={to_addr} from={MAIL_FROM_A} rcpt={c2} data={c3} resp={r3!r}")
        if c3 >= 400:
            raise RuntimeError(f"data rejected: {c3} {r3!r}")
        try:
            s.quit()
        except Exception:
            pass
    return True

def log_line(msg):
    try:
        with open(os.path.join(DATA_DIR, "mail.log"), "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except Exception:
        pass

# ---------- OMB ----------
def omb_get(path):
    with urlopen(OMB + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def omb_post(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = Request(OMB + path, data=data,
                  headers={"content-type": "application/json"}, method="POST")
    with urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8") or "{}")

def bot_turns_since(since_ms):
    """Turnos del bot posteriores a since_ms.

    Por turno: `first` (primer texto = acuse), `last` (ultimo texto = respuesta final)
    y `ended` (llego su 'digest'). Asi el chat puede publicar el acuse enseguida y la
    respuesta completa al cerrar el turno.
    """
    try:
        d = omb_get(f"/api/threads/{THREAD_ID}/messages?limit=120")
    except Exception:
        return {}
    turns = {}
    for m in d.get("messages", []):
        if m.get("role") != "bot":
            continue
        tid = m.get("turnId") or ""
        if not tid:
            continue
        kind = m.get("kind")
        at = int(m.get("at", 0) or 0)
        t = turns.setdefault(tid, {"first": "", "first_at": 0,
                                   "last": "", "last_at": 0, "ended": False,
                                   "final": ""})
        if kind == "digest":
            t["ended"] = True
        elif kind == "text":
            body = (m.get("text") or "").strip()
            if body:
                if at > since_ms and not t["first"]:
                    t["first"], t["first_at"] = body, at
                if at > since_ms:
                    t["last"], t["last_at"] = body, at
                if m.get("turnTerminal"):
                    t["final"] = body
    return {k: v for k, v in turns.items() if v["last"]}

MIRROR_GRACE   = 150   # s de silencio total tras el ultimo texto antes de cerrar el espejo
MIRROR_MAX     = 1800  # s de tope duro del espejo (red de seguridad)
MIRROR_POLL    = 2     # s entre lecturas del hilo
MIRROR_SETTLE  = 8     # s de quietud antes de publicar (evita cortar a mitad)

def _publish(role, text):
    """Publica un mensaje del bot en el chat (si no estaba ya) y avisa por push."""
    if not text or not text.strip():
        return
    if already_sent(role, text):
        return
    add_msg(role, text)
    threading.Thread(target=send_push, args=("Manager", text), daemon=True).start()

def _mirror_follow(t0):
    """Espeja el hilo del agente AL MARGEN del dispatch.

    El backend solo copiaba los mensajes anteriores al disparo del turno y dejaba de
    mirar al llegar el 'digest'; en turnos largos la respuesta final (posterior al
    digest) se perdia. Este hilo lee el hilo hasta que queda en silencio, y publica
    cualquier texto del bot que no este todavia en el chat.
    """
    grace = MIRROR_GRACE
    hard = time.time() + MIRROR_MAX
    try:
        while time.time() < hard:
            time.sleep(MIRROR_POLL)
            try:
                turns = bot_turns_since(t0)
            except Exception:
                continue
            if not turns:
                continue
            for t in sorted(turns.values(), key=lambda x: x["last_at"]):
                _publish("bot", t.get("final") or "")
            now = now_ms()
            idle = (now - max((t["last_at"] for t in turns.values()), default=now)) / 1000.0
            if idle < 0:
                idle = 0
            if idle >= grace:
                break
    except Exception as e:
        print("mirror error:", e, flush=True)
    finally:
        _mirror["on"] = False

def _start_mirror(t0):
    if _mirror["on"]:
        return
    _mirror["on"] = True
    threading.Thread(target=_mirror_follow, args=(t0,), daemon=True).start()

def send_push(title, body):
    if not (_HAS_PUSH and VAPID.get("private_key")):
        return
    try:
        with db() as c:
            subs = [r["sub"] for r in c.execute("SELECT sub FROM push_subs").fetchall()]
    except Exception:
        return
    payload = json.dumps({"title": title, "body": body or ""})
    for sub in subs:
        try:
            info = json.loads(sub)
            webpush(subscription_info=info, data=payload,
                    vapid_private_key=VAPID["private_key"],
                    vapid_claims={"sub": VAPID_SUB})
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                try:
                    with _dblock, db() as c:
                        c.execute("DELETE FROM push_subs WHERE sub=?", (sub,))
                except Exception:
                    pass
        except Exception:
            pass

def dispatch(text, uid=None):
    """Guarda el mensaje del usuario, dispara el turno del agente y espera su respuesta.

    Serializado con `_dispatch_lock`: si dos turnos se solapan, el segundo espera a que
    el primero cierre. Sin esto, dos hilos leen los mismos mensajes del bot y publican la
    respuesta por duplicado (bug observado 2026-10-08).
    """
    with _dispatch_lock:
        _busy["on"] = True
        _busy["since"] = now_ms()
        _busy["q"] += 1
        try:
            _dispatch_locked(text, uid)
        finally:
            _busy["q"] -= 1
            if _busy["q"] <= 0:
                _busy["q"] = 0
                _busy["on"] = False

def _dispatch_locked(text, uid=None):
    add_msg("user", text, uid)
    t0 = now_ms()
    try:
        omb_post(f"/api/bots/{BOT_ID}/messages", {"text": text, "threadId": THREAD_ID})
    except Exception as e:
        add_msg("system", f"No se pudo disparar el turno: {e}")
        return
    _start_mirror(t0)          # red de seguridad: publica lo que el dispatch no copie
    deadline = time.time() + 600
    acked = ""
    last_seen = ""
    stable = 0
    while time.time() < deadline:
        time.sleep(1)
        turns = bot_turns_since(t0)
        if not turns:
            continue
        ordered = [t for _, t in sorted(turns.items(), key=lambda x: x[1]["last_at"])]

        # 1) acuse rapido: primer texto del turno, en cuanto aparece
        if not acked and ordered[0]["first"]:
            acked = ordered[0]["first"]
            add_msg("bot", acked)

        # 2) respuesta final: al cerrarse el turno (llega su digest)
        ready = [t for t in ordered if t["ended"]]
        if ready:
            final = "\n\n".join((t.get("final") or t["last"]) for t in ready)
            if final.strip() and final.strip() != acked.strip():
                if not already_sent("bot", final):
                    add_msg("bot", final)
                    threading.Thread(target=send_push, args=("Manager", final), daemon=True).start()
            elif final.strip() and not already_sent("bot", final):
                threading.Thread(target=send_push, args=("Manager", final), daemon=True).start()
            return

        # 3) respaldo: hay texto pero el turno no cierra; si esta quieto 45 s, publicar
        cand = "\n\n".join((t.get("final") or t["last"]) for t in ordered)
        if cand == last_seen:
            stable += 1
        else:
            stable = 0
            last_seen = cand
        if cand.strip() and cand.strip() != acked.strip() and stable >= 45:
            add_msg("bot", cand)
            threading.Thread(target=send_push, args=("Manager", cand), daemon=True).start()
            return
    if last_seen.strip() and last_seen.strip() != acked.strip():
        add_msg("bot", last_seen)
        threading.Thread(target=send_push, args=("Manager", last_seen), daemon=True).start()
    else:
        add_msg("system", "Manager no respondio a tiempo. El mensaje esta guardado.")

# ---------- http ----------
def parse_cookies(h):
    out = {}
    for part in (h or "").split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out

class Handler(BaseHTTPRequestHandler):
    server_version = "chat/1.0"

    def log_message(self, fmt, *a):
        syslog = f"{self.address_string()} {fmt % a}"
        try:
            with open(os.path.join(DATA_DIR, "access.log"), "a") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {syslog}\n")
        except Exception:
            pass

    # ---- helpers ----
    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(b)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _bytes(self, code, b, ctype, extra=None, disp=None):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b)))
        self.send_header("cache-control", "no-store")
        if disp:
            self.send_header("content-disposition", disp)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("content-length") or 0)
        if n <= 0:
            return b""
        if n > MAX_UPLOAD + 1024:
            raise ValueError("body too large")
        return self.rfile.read(n)

    def _session_ok(self):
        sid = parse_cookies(self.headers.get("cookie")).get("chat_session")
        if not sid:
            return False
        with db() as c:
            row = c.execute("SELECT expires FROM sessions WHERE sid=?", (sid,)).fetchone()
        return bool(row) and row["expires"] > time.time()

    def _require(self):
        if self._session_ok():
            return True
        self._json(401, {"error": "no-session"})
        return False

    # ---- routing ----
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self._static("index.html")
            if p in ("/manifest.webmanifest", "/sw.js", "/theme.css", "/app.js", "/icon.svg", "/icon-512.png", "/apple-touch-icon.png"):
                return self._static(p.lstrip("/"))
            if p == "/api/me":
                return self._json(200, {"ok": self._session_ok()})
            if p == "/api/push/key":
                if not self._require():
                    return
                return self._json(200, {"key": VAPID.get("public_key")})
            if p == "/api/history":
                if not self._require():
                    return
                q = parse_qs(u.query)
                before = q.get("before", [None])[0]
                lim = min(int(q.get("limit", ["60"])[0] or 60), 200)
                return self._json(200, {"messages": history(lim, before),
                                         "busy": bool(_busy["on"]),
                                         "busySince": int(_busy["since"] or 0)})
            if p.startswith("/api/file/"):
                if not self._require():
                    return
                fid = p.rsplit("/", 1)[-1]
                return self._file(fid)
            return self._json(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        try:
            if p == "/api/request-token":
                return self._request_token()
            if p == "/api/verify":
                return self._verify()
            if p == "/api/logout":
                return self._logout()
            if p == "/api/upload":
                if not self._require():
                    return
                return self._upload()
            if p == "/api/push/subscribe":
                if not self._require():
                    return
                return self._push_subscribe()
            if p == "/api/push/unsubscribe":
                if not self._require():
                    return
                return self._push_unsubscribe()
            if p == "/api/push/status":
                if not self._require():
                    return
                return self._push_status()
            if p == "/api/push/test":
                if not self._require():
                    return
                threading.Thread(target=send_push, args=("Manager", "Prueba de notificacion"), daemon=True).start()
                return self._json(200, {"ok": True})
            if p == "/api/send":
                if not self._require():
                    return
                return self._send()
            return self._json(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"error": str(e)})

    # ---- static ----
    def _static(self, name):
        path = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(path):
            return self._json(404, {"error": "missing"})
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            b = f.read()
        extra = {}
        if name in ("sw.js", "index.html"):
            extra["cache-control"] = "no-store"
        return self._bytes(200, b, ctype, extra)

    # ---- auth ----
    def _client_ip(self):
        return (self.headers.get("cf-connecting-ip")
                or (self.headers.get("x-forwarded-for") or "").split(",")[0].strip()
                or self.client_address[0])

    def _rate_ok(self, key, limit, window):
        with _dblock, db() as c:
            row = c.execute("SELECT n,ts FROM rl WHERE k=?", (key,)).fetchone()
            if not row or time.time() - row["ts"] > window:
                c.execute("INSERT OR REPLACE INTO rl(k,n,ts) VALUES(?,1,?)", (key, int(time.time())))
                return True
            if row["n"] >= limit:
                return False
            c.execute("UPDATE rl SET n=n+1 WHERE k=?", (key,))
            return True

    def _request_token(self):
        ip = self._client_ip()
        if not self._rate_ok("ip:" + ip, 6, 3600):
            return self._json(429, {"error": "too many requests"})
        body = json.loads(self._body().decode("utf-8") or "{}")
        email = (body.get("email") or OWNER or "").strip()
        if not OWNER or email.lower() != OWNER:
            time.sleep(1.0)
            return self._json(200, {"ok": True})
        tok = secrets.token_urlsafe(18)
        with _dblock, db() as c:
            c.execute("INSERT INTO logins(token,created,expires,used) VALUES(?,?,?,0)",
                      (tok, int(time.time()), int(time.time()) + TOKEN_MIN * 60))
        try:
            send_token_email(OWNER, tok)
        except Exception as e:
            traceback.print_exc()
            return self._json(502, {"error": "mail failed"})
        return self._json(200, {"ok": True})

    def _verify(self):
        ip = self._client_ip()
        if not self._rate_ok("verify:" + ip, 20, 3600):
            return self._json(429, {"error": "too many requests"})
        body = json.loads(self._body().decode("utf-8") or "{}")
        tok = (body.get("token") or "").strip()
        with _dblock, db() as c:
            row = c.execute("SELECT expires,used FROM logins WHERE token=?", (tok,)).fetchone()
            if not row or row["used"] or row["expires"] < time.time():
                time.sleep(0.8)
                return self._json(401, {"error": "invalid"})
            c.execute("UPDATE logins SET used=1 WHERE token=?", (tok,))
            sid = secrets.token_urlsafe(32)
            c.execute("INSERT INTO sessions(sid,created,expires) VALUES(?,?,?)",
                      (sid, int(time.time()), int(time.time()) + SESSION_DAYS * 86400))
        b = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.send_header("set-cookie",
                         f"chat_session={sid}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age={SESSION_DAYS*86400}")
        self.end_headers()
        self.wfile.write(b)

    def _logout(self):
        sid = parse_cookies(self.headers.get("cookie")).get("chat_session")
        if sid:
            with _dblock, db() as c:
                c.execute("DELETE FROM sessions WHERE sid=?", (sid,))
        b = b'{"ok":true}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.send_header("set-cookie", "chat_session=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0")
        self.end_headers()
        self.wfile.write(b)

    # ---- chat ----
    def _send(self):
        body = json.loads(self._body().decode("utf-8") or "{}")
        text = (body.get("text") or "").strip()
        atts = body.get("attachments") or []
        if not text and not atts:
            return self._json(400, {"error": "empty"})
        if len(text) > 20000:
            return self._json(413, {"error": "too long"})
        lines = []
        if text:
            lines.append(text)
        for fid in atts:
            with db() as c:
                r = c.execute("SELECT name,mime,size FROM files WHERE id=?", (fid,)).fetchone()
            if r:
                lines.append(f"[adjunto] {r['name']} ({r['mime']}, {r['size']} B) -> /api/file/{fid}")
        payload = "\n".join(lines)
        threading.Thread(target=dispatch, args=(payload,), daemon=True).start()
        return self._json(200, {"ok": True, "accepted": True})

    def _push_subscribe(self):
        body = json.loads(self._body().decode("utf-8") or "{}")
        sub = body.get("subscription")
        if not isinstance(sub, dict) or not sub.get("endpoint"):
            return self._json(400, {"error": "bad subscription"})
        blob = json.dumps(sub)
        with _dblock, db() as c:
            c.execute("INSERT OR REPLACE INTO push_subs(endpoint,sub,created) VALUES(?,?,?)",
                      (sub["endpoint"], blob, now_ms()))
        return self._json(200, {"ok": True})

    def _push_status(self):
        body = json.loads(self._body().decode("utf-8") or "{}")
        endpoint = body.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            return self._json(200, {"registered": False})
        with db() as c:
            r = c.execute("SELECT 1 FROM push_subs WHERE endpoint=?", (endpoint,)).fetchone()
        return self._json(200, {"registered": bool(r)})

    def _push_unsubscribe(self):
        body = json.loads(self._body().decode("utf-8") or "{}")
        endpoint = body.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            return self._json(400, {"error": "bad endpoint"})
        with _dblock, db() as c:
            c.execute("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))
        return self._json(200, {"ok": True})

    def _file(self, fid):
        if not re.fullmatch(r"[0-9a-f]{32}", fid or ""):
            return self._json(404, {"error": "not found"})
        with db() as c:
            r = c.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not r:
            return self._json(404, {"error": "not found"})
        path = os.path.join(FILES_DIR, fid)
        if not os.path.isfile(path):
            return self._json(404, {"error": "gone"})
        with open(path, "rb") as f:
            b = f.read()
        inline = r["mime"].startswith("image/") or r["mime"].startswith("text/") or r["mime"] == "application/pdf"
        cd = ("inline" if inline else "attachment")
        disp = f'{cd}; filename="{quote(r["name"])}"'
        return self._bytes(200, b, r["mime"] or "application/octet-stream", disp=disp)

    def _upload(self):
        ctype = self.headers.get("content-type") or ""
        if "multipart/form-data" not in ctype:
            return self._json(400, {"error": "expected multipart"})
        raw = self._body()
        if len(raw) > MAX_UPLOAD + 4096:
            return self._json(413, {"error": "too large"})
        m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', ctype)
        if not m:
            return self._json(400, {"error": "no boundary"})
        boundary = (m.group(1) or m.group(2)).strip().encode()
        parts = raw.split(b"--" + boundary)
        fname, fdata, fmime = None, None, "application/octet-stream"
        for part in parts:
            if b"\r\n\r\n" not in part:
                continue
            head, _, data = part.partition(b"\r\n\r\n")
            if b'name="file"' not in head:
                continue
            if data.endswith(b"\r\n"):
                data = data[:-2]
            fdata = data
            hm = re.search(rb'filename="([^"]*)"', head)
            if hm:
                fname = hm.group(1).decode("utf-8", "replace")
            cm = re.search(rb"Content-Type:\s*([^\r\n]+)", head, re.I)
            if cm:
                fmime = cm.group(1).decode("utf-8", "replace").strip()
            break
        if fdata is None:
            return self._json(400, {"error": "no file"})
        fid = uuid.uuid4().hex
        with open(os.path.join(FILES_DIR, fid), "wb") as f:
            f.write(fdata)
        safe = re.sub(r"[^\w.\-() ]", "_", (fname or "adjunto"))[:120] or "adjunto"
        with _dblock, db() as c:
            c.execute("INSERT INTO files(id,name,mime,size,created) VALUES(?,?,?,?,?)",
                      (fid, safe, fmime, len(fdata), now_ms()))
        return self._json(200, {"id": fid, "name": safe, "mime": fmime,
                                "size": len(fdata), "url": f"/api/file/{fid}"})

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def recover_pending():
    """Si el ultimo mensaje es del usuario y quedo sin respuesta (p.ej. un reinicio
    mato el turno en curso), lo re-dispara al arrancar."""
    try:
        with db() as c:
            last = c.execute("SELECT role, content, at FROM messages ORDER BY at DESC LIMIT 1").fetchone()
    except Exception:
        return
    if not last or last["role"] != "user":
        return
    if (time.time() * 1000 - int(last["at"] or 0)) < 8000:
        return
    print("recuperando turno perdido:", (last["content"] or "")[:60], flush=True)
    threading.Thread(target=dispatch, args=(last["content"],), daemon=True).start()

if __name__ == "__main__":
    init_db()
    recover_pending()
    _start_mirror(now_ms() - 30 * 60 * 1000)   # ultimos 30 min, por si quedo algo sin publicar
    print(f"chat backend on 127.0.0.1:{PORT}  data={DATA_DIR}", flush=True)
    Server(("127.0.0.1", PORT), Handler).serve_forever()
