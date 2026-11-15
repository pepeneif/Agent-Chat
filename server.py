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
import unicodedata, subprocess, shutil, tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote
from urllib.error import HTTPError
import contextlib
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
DATA_DIR   = os.environ.get("CHAT_DATA", os.path.expanduser("~/chat/data"))
STATIC_DIR = os.environ.get("CHAT_STATIC", os.path.expanduser("~/chat/static"))
PORT       = int(os.environ.get("CHAT_PORT", "8792"))
OMB        = os.environ.get("CHAT_OMB", "http://127.0.0.1:8799")
BOT_ID     = os.environ.get("CHAT_BOT_ID", "")
THREAD_ID  = os.environ.get("CHAT_THREAD_ID", "")
OWNER      = (os.environ.get("CHAT_OWNER", "")).strip().lower()
BRAND      = os.environ.get("CHAT_BRAND", "Agent Chat").strip() or "Agent Chat"
MAIL_FROM_H = os.environ.get("CHAT_MAIL_NAME", BRAND)
MAIL_FROM_A = os.environ.get("CHAT_MAIL_FROM", "no-reply@example.com")
MAIL_FROM_D = os.environ.get("CHAT_MAIL_DOMAIN", MAIL_FROM_A.split("@", 1)[1] if "@" in MAIL_FROM_A else "example.com")
SMTP_HOST  = os.environ.get("SMTP_HOST", "127.0.0.1")
SMTP_PORT  = int(os.environ.get("SMTP_PORT", "25"))
SESSION_DAYS = 30
TOKEN_MIN    = 15
MAX_UPLOAD   = 25 * 1024 * 1024

# Tope global de generacion de codigos, independiente del limite por IP. Evita
# inundar el buzon del dueno con IPs rotativas. Configurable con la variable de
# entorno CHAT_TOKEN_LIMIT_PER_HOUR; el valor sin configurar es deliberadamente bajo.
TOKEN_GEN_LIMIT_PER_HOUR = int(os.environ.get("CHAT_TOKEN_LIMIT_PER_HOUR", "10"))

# Opcion B: una sola burbuja "en curso" por turno de Manager, actualizada en silencio
# durante el turno, con un push al nacer y otro forzado al cerrar. Desactivada por
# defecto para no alterar la via A (mirror actual); se activa con CHAT_OPTION_B=1.
OPTION_B = os.environ.get("CHAT_OPTION_B", "").strip().lower() in ("1", "true", "yes", "on")
OPTION_B_START_PUSH = os.environ.get("CHAT_B_START_PUSH", BRAND + " esta trabajando...").strip()

# Control de turno del usuario (F1-F4, optativo): steer nativo de OMB cuando el bot esta
# ocupado, boton "Detener" (interrupt) y estado "trabajando" leido del OMB real. Off por
# defecto: con CHAT_TURN_CONTROL apagado la via A queda byte-identica a la base.
TURN_CONTROL = os.environ.get("CHAT_TURN_CONTROL", "").strip().lower() in ("1", "true", "yes", "on")
DETENER_DEFAULT = "detener el turno actual"   # texto steer del boton Detener (sin mensaje propio)

def _is_stop_text(text):
    return bool(re.fullmatch(r"[\s/!.,]*(para|parar|stop|detente|detener|cancela|cancelar|alto)[\s!.]*",
                             (text or "").strip(), re.I))

os.makedirs(DATA_DIR, exist_ok=True)
OMB_ATTACH_CONTAINER = os.environ.get("CHAT_OMB_CONTAINER", "").strip()
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
_debuging = {"on": False}   # hay una depuracion en curso
_viewer = {"last": 0, "visible": True, "at_bottom": True, "viewer_at": 0}   # visibilidad real de la UI (POST /api/view)
VIEWER_TTL_MS = 25000       # margen: si hubo poll hace <25s, no mandamos push (esta leyendo)

# H7: el estado "trabajando" real de OMB se sirve desde una cache de TTL corto y se
# refresca con un timeout dedicado (<=3s), NUNCA con el omb_get generico de 30 s sobre
# el hilo de /api/history. Sin cache fresca devuelve el fallback local _busy["on"].
BUSY_CACHE_TTL_MS = 2500
BUSY_FETCH_TIMEOUT = 3.0
# H8: si el hilo de refresco muere sin su finally (muerte abrupta del thread/interprete),
# `refreshing` quedaria True y la cache dejaria de actualizarse. Pasado este margen >
# BUSY_FETCH_TIMEOUT (y holgado frente a un refresco normal de <=3 s), lo damos por
# muerto y permitimos lanzar uno nuevo. 12000 ms = 4x el timeout tipico: suficiente
# margen sin arrastrar un valor viejo indefinidamente.
BUSY_REFRESH_STUCK_MS = 12000
_busy_cache = {"value": None, "at": 0, "refreshing": False, "refreshing_since": 0}
_busy_cache_lock = threading.Lock()

# Espera de delegaciones (waits bots): el agente delego en otros bots con coordinate_bots.
# Se ingiere de los mensajes kind=="activity" del stream OMB. Matcher tolerante a
# version/idioma: "coordinate_bots" o "^Sent to X" abre (y registra peer X); "^X replied"
# cierra a ese peer. TTL de seguridad para no dejar el estado colgado si no llega reply.
WAIT_PEER_RE = re.compile(r"^sent\s+to\s+(.+)$", re.I)
WAIT_REPLIED_RE = re.compile(r"^(.+?)\s+replied$", re.I)
WAIT_STUCK_MS = 15 * 60 * 1000
_waiting_bots = {"since": None, "peers": []}
_waiting_bots_lock = threading.Lock()

# ---------- db ----------
@contextlib.contextmanager
def db():
    # OJO: el "with" de sqlite3 NO cierra la conexion (solo commit/rollback). Por eso este
    # db() es un context manager explicito que SI cierra: sin esto cada consulta filtraba un
    # descriptor de fichero (~510 por dia en el mirror) hasta agotar el limite de 1024 y
    # dejar el backend mudo (502). Bug encontrado el 2026-10-14 con helma.merkle.space.
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY, role TEXT, content TEXT, at INTEGER, uid TEXT,
            live INTEGER NOT NULL DEFAULT 0,
            subturn TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS logins (
            token TEXT PRIMARY KEY, created INTEGER, expires INTEGER, used INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessions (
            sid TEXT PRIMARY KEY, created INTEGER, expires INTEGER);
        CREATE TABLE IF NOT EXISTS files (
            id TEXT PRIMARY KEY, name TEXT, mime TEXT, size INTEGER, created INTEGER);
        CREATE TABLE IF NOT EXISTS rl (k TEXT PRIMARY KEY, n INTEGER, ts INTEGER);
        CREATE TABLE IF NOT EXISTS push_subs (endpoint TEXT PRIMARY KEY, sub TEXT, created INTEGER);
        CREATE TABLE IF NOT EXISTS prefs (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS omb_deliveries (source_id TEXT PRIMARY KEY, message_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS omb_pending_turns (
            turn_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, message_at INTEGER NOT NULL,
            push_eligible INTEGER NOT NULL, next_check_at INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS omb_push_events (
            turn_id TEXT PRIMARY KEY, source_id TEXT NOT NULL UNIQUE,
            message_at INTEGER NOT NULL, attempted_at INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS omb_option_b_turns (
            turn_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, started_at INTEGER NOT NULL,
            start_push_at INTEGER, closed_at INTEGER, final_source_id TEXT,
            next_check_at INTEGER NOT NULL DEFAULT 0, last_source_id TEXT);
        CREATE TABLE IF NOT EXISTS omb_push_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT, turn_id TEXT NOT NULL, phase TEXT NOT NULL,
            body TEXT NOT NULL, at INTEGER NOT NULL, UNIQUE(turn_id, phase));
        CREATE TABLE IF NOT EXISTS omb_user_deliveries (
            source_id TEXT PRIMARY KEY, message_id TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_messages_uid ON messages(uid);
        """)
        columns = {row[1] for row in c.execute("PRAGMA table_info(omb_push_events)")}
        if "attempts" not in columns:
            c.execute("ALTER TABLE omb_push_events ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        if "next_attempt_at" not in columns:
            c.execute("ALTER TABLE omb_push_events ADD COLUMN next_attempt_at INTEGER NOT NULL DEFAULT 0")
        mcols = {row[1] for row in c.execute("PRAGMA table_info(messages)")}
        if "live" not in mcols:
            c.execute("ALTER TABLE messages ADD COLUMN live INTEGER NOT NULL DEFAULT 0")
        if "subturn" not in mcols:
            c.execute("ALTER TABLE messages ADD COLUMN subturn TEXT NOT NULL DEFAULT ''")
        bcols = {row[1] for row in c.execute("PRAGMA table_info(omb_option_b_turns)")}
        if "next_check_at" not in bcols:
            c.execute("ALTER TABLE omb_option_b_turns ADD COLUMN next_check_at INTEGER NOT NULL DEFAULT 0")
        if "last_source_id" not in bcols:
            c.execute("ALTER TABLE omb_option_b_turns ADD COLUMN last_source_id TEXT")
    # H9 (cabo 1): rellena last_source_id de turnos B abiertos sin el usando el source_id
    # de mayor rowid en omb_deliveries para su message_id. Las filas creadas antes de la
    # migracion (columnas anadidas despues) quedaban con NULL y se saltaban sin cerrar.
    with db() as c:
        c.execute("""
            UPDATE omb_option_b_turns
            SET last_source_id = (
                SELECT d.source_id FROM omb_deliveries d
                WHERE d.message_id = omb_option_b_turns.message_id
                ORDER BY d.rowid DESC LIMIT 1
            )
            WHERE last_source_id IS NULL AND closed_at IS NULL
              AND EXISTS (
                SELECT 1 FROM omb_deliveries d WHERE d.message_id = omb_option_b_turns.message_id
              )
        """)

def now_ms(): return int(time.time() * 1000)

def get_pref(k, default=None):
    try:
        with db() as c:
            r = c.execute("SELECT v FROM prefs WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default
    except Exception:
        return default

def set_pref(k, v):
    with _dblock, db() as c:
        c.execute("INSERT OR REPLACE INTO prefs(k,v) VALUES(?,?)", (k, str(v)))

def add_msg(role, content, uid=None):
    mid = uuid.uuid4().hex
    with _dblock, db() as c:
        c.execute("INSERT INTO messages(id,role,content,at,uid) VALUES(?,?,?,?,?)",
                  (mid, role, content, now_ms(), uid))
    return {"id": mid, "role": role, "content": content, "at": now_ms()}

def history(limit=60, before=None, after=None):
    with db() as c:
        if after:
            rows = c.execute("SELECT * FROM messages WHERE at>? ORDER BY at ASC LIMIT ?",
                             (int(after), limit)).fetchall()
            return [dict(r) for r in rows]
        if before:
            rows = c.execute("SELECT * FROM messages WHERE at<? ORDER BY at DESC LIMIT ?",
                             (int(before), limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM messages ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
    return list(reversed([dict(r) for r in rows]))

# ---------- busqueda (2026-10-15) ----------
# Busca en el historial del chat (lo que NO se ha depurado: lo borrado ya no existe) y,
# opcionalmente, en la wiki de memoria del bot (memory/*.md dentro del volumen /data de
# OMB). La wiki se extrae en SOLO LECTURA con `docker exec <contenedor> tar` (el usuario
# del servicio esta en el grupo docker), a un directorio temporal con cache de TTL. Nada
# se escribe de vuelta ni se modifica ningun permiso. Apagable con CHAT_SEARCH=0.

def _fold_char(ch):
    """Pliega un caracter a minuscula sin diacriticos, MANTENIENDO la longitud (1 char
    -> 1 char) para que los indices del texto plegado coincidan con el original."""
    d = unicodedata.normalize("NFD", ch)   # NFD (no NFKD): el frontal pliega igual (H1 auditoria 2026-10-15)
    base = "".join(c for c in d if not unicodedata.combining(c))
    if len(base) == 1:
        return base.lower()
    return ch.lower() if len(ch) == 1 else ch

def _fold(text):
    return "".join(_fold_char(ch) for ch in (text or ""))

def _occurrences(hay, needle):
    """Posiciones no solapadas de `needle` en `hay` (ambos ya plegados)."""
    out = []
    if not needle:
        return out
    start = 0
    while True:
        i = hay.find(needle, start)
        if i < 0:
            break
        out.append(i)
        start = i + len(needle)
    return out

def _snippet(text, pos, qlen, width=60):
    a = max(0, pos - width)
    b = min(len(text), pos + qlen + width)
    frag = text[a:b].replace("\n", " ").strip()
    return ("\u2026" if a > 0 else "") + frag + ("\u2026" if b < len(text) else "")

def search_chat(q):
    nq = _fold(q)
    if not nq:
        return []
    out = []
    with db() as c:
        cur = c.execute("SELECT id,role,content,at FROM messages ORDER BY at,id")
        for r in cur:   # iterar el cursor: no cargar toda la tabla en RAM (H2 auditoria)
            content = r["content"] or ""
            occ = _occurrences(_fold(content), nq)
            if not occ:
                continue
            if r["role"] == "system":
                kind = "system"
            elif r["role"] == "user":
                kind = "user"
            else:
                kind = "bot"
            out.append({"id": r["id"], "role": r["role"], "kind": kind, "at": r["at"],
                        "count": len(occ), "snippet": _snippet(content, occ[0], len(nq))})
            if len(out) >= SEARCH_LIMIT:
                break
    return out

def _wiki_files(force=False):
    """Devuelve la wiki cacheada (lista de {path, lines, folded} con las lineas ya
    plegadas). NUNCA bloquea la peticion con la extraccion: si no hay cache, dispara el
    refresco en segundo plano y devuelve [] (el siguiente intento ya la tendra)."""
    now = time.time()
    if _wiki["files"] is not None and (now - _wiki["at"]) < WIKI_CACHE_TTL and not force:
        return _wiki["files"]
    _wiki_kick()          # refresco asincrono (una sola vez a la vez)
    return _wiki["files"] or []

def _wiki_kick():
    with _wiki["lock"]:
        if _wiki["loading"]:
            return
        _wiki["loading"] = True
    threading.Thread(target=_wiki_build, daemon=True, name="chat-wiki").start()

def _wiki_build():
    try:
        files = _wiki_extract()
        with _wiki["lock"]:
            _wiki["files"] = files
            _wiki["at"] = time.time()
            _wiki["loading"] = False
    except Exception:
        with _wiki["lock"]:
            _wiki["loading"] = False

def _wiki_prewarm():
    """Precalienta la wiki al arrancar y la refresca sola cada WIKI_CACHE_TTL/2."""
    while True:
        _wiki_build_refresh()
        time.sleep(max(60, WIKI_CACHE_TTL // 2))

def _wiki_build_refresh():
    try:
        files = _wiki_extract()
        with _wiki["lock"]:
            _wiki["files"] = files
            _wiki["at"] = time.time()
    except Exception:
        pass

def _wiki_extract():
    """Extrae en SOLO LECTURA los .md de `WIKI_DIRS` bajo `WIKI_WORKSPACE` del volumen
    /data de OMB, usando `docker exec <ctr> tar` a un tmp. Nunca escribe en el volumen.
    Devuelve [{path, lines, folded}] con el texto de cada linea y su version plegada."""
    tmp = None
    try:
        if not WIKI_CONTAINER or not shutil.which("docker"):
            return []
        rel = os.path.relpath(WIKI_WORKSPACE, "/data")
        if rel.startswith("..") or os.path.isabs(rel):
            return []
        tmp = tempfile.mkdtemp(prefix="chatwiki-")
        proc = subprocess.Popen(
            ["docker", "exec", WIKI_CONTAINER, "tar", "-C", "/data", "-cf", "-", "./" + rel],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            subprocess.run(["tar", "-C", tmp, "-xf", "-"], stdin=proc.stdout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=True, timeout=WIKI_EXTRACT_TIMEOUT)   # H4: tope global
        finally:
            proc.stdout.close()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        base = os.path.join(tmp, rel)
        out = []
        for d in WIKI_DIRS:
            root = os.path.join(base, d)
            for dp, _dirs, names in os.walk(root):
                for nm in names:
                    if not nm.endswith(".md"):
                        continue
                    fp = os.path.join(dp, nm)
                    try:
                        with open(fp, encoding="utf-8", errors="replace") as f:
                            txt = f.read()
                    except Exception:
                        continue
                    lines = txt.split("\n")
                    out.append({"path": os.path.relpath(fp, base),
                                "lines": lines,
                                "folded": [_fold(x) for x in lines]})
        return out
    except Exception:
        return []
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

def search_wiki(q, max_hits=15):
    nq = _fold(q)
    if not nq:
        return []
    out = []
    for f in _wiki_files():
        hits = []
        total = 0
        for i, fl in enumerate(f["folded"]):
            occ = _occurrences(fl, nq)
            if occ:
                total += len(occ)
                if len(hits) < max_hits:
                    hits.append({"line": i + 1, "text": (f["lines"][i] or "").strip()[:200]})
        if total:
            out.append({"path": f["path"], "count": total, "hits": hits})
    return out


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

# F1 (S-2026-11-15): aislar SMTP para que /api/request-token responda con el mismo
# coste tanto si el email coincide como si no. El fallo SMTP se registra en mail.log
# y NUNCA se devuelve como 502 al cliente.
def _send_token_safely(to_addr, token):
    try:
        send_token_email(to_addr, token)
    except Exception as e:
        log_line(f"MAIL FALLO {e} to={to_addr}")

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

DEBUG_KEEP = int(os.environ.get("CHAT_DEBUG_KEEP", "40"))   # legado; sustituido por CHAT_PURGE_PCT
PURGE_PCT = float(os.environ.get("CHAT_PURGE_PCT", "66"))

# --- Busqueda (2026-10-15) -------------------------------------------------
# Busca en el historial del chat (no depurado) y, opcionalmente, en la wiki de memoria
# del bot (archivos memory/*.md dentro del volumen /data de OMB). El backend corre como
# usuario del host, fuera del contenedor, asi que la wiki se extrae en SOLO LECTURA con
# `docker exec ... tar` (el usuario esta en el grupo docker). Sin persistir nada fuera de
# una cache temporal con TTL. Todo apagable con CHAT_SEARCH=0.
SEARCH_ON    = os.environ.get("CHAT_SEARCH", "1").strip().lower() in ("1", "true", "yes", "on")
SEARCH_MIN   = int(os.environ.get("CHAT_SEARCH_MIN", "2"))       # letras minimas para lanzar
SEARCH_LIMIT = int(os.environ.get("CHAT_SEARCH_LIMIT", "400"))   # tope de coincidencias chat
WIKI_CONTAINER = os.environ.get("CHAT_WIKI_CONTAINER", "").strip()
WIKI_WORKSPACE = os.environ.get("CHAT_WIKI_WORKSPACE",
                                "")
WIKI_DIRS    = [d.strip() for d in os.environ.get("CHAT_WIKI_DIRS", "memory").split(",") if d.strip()]
WIKI_CACHE_TTL = int(os.environ.get("CHAT_WIKI_CACHE_TTL", "300"))  # segundos
WIKI_EXTRACT_TIMEOUT = int(os.environ.get("CHAT_WIKI_TIMEOUT", "45"))  # tope de la extraccion
_wiki = {"at": 0, "files": None, "loading": False, "lock": threading.Lock()}

def _debug_busy(on):
    """Marca/desmarca el flag de trabajo (para que el chat muestre 'Manager esta trabajando')."""
    if on:
        _busy["q"] += 1
        _busy["on"] = True
        # H10: el trabajo de depuracion/compactacion NO es un turno de chat. No fijamos
        # _busy["since"] aqui: ese ancla queda reservada para el turno en curso
        # (_mark_busy) o para la burbuja viva de OMB (_busy_since_ms). Asi el contador
        # "(Ns)" no arranca desde el principio del debug_worker ni acumula entre tasks.
    else:
        _busy["q"] = max(0, _busy["q"] - 1)
        if _busy["q"] == 0:
            _busy["on"] = False
            _busy["since"] = 0

def _omb_busy():
    """True/False si el bot esta ocupado; None si no se pudo saber.

    Consulta generica con timeout largo: valida para background/steer/dispatch, NO para
    /api/history (ver _omb_busy_fetch)."""
    try:
        d = omb_get("/api/bots")
        for x in d.get("bots", []):
            if x.get("id") == BOT_ID:
                return bool(x.get("busy"))
    except Exception:
        pass
    return None


def _omb_busy_fetch(timeout=BUSY_FETCH_TIMEOUT):
    """Consulta /api/bots con timeout corto dedicado (H7). No usa omb_get."""
    try:
        with urlopen(OMB + "/api/bots", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        for x in d.get("bots", []):
            if x.get("id") == BOT_ID:
                return bool(x.get("busy"))
    except Exception:
        pass
    return None

def _content_lines(content):
    """Numero de lineas de un mensaje. Linea = segmento separado por saltos de linea."""
    content = content or ""
    return 0 if not content else len(content.split("\n"))

def purge_history(pct=None):
    """Poda ~`pct`% de las LINEAS mas antiguas del chat, cortando en frontera de
    turno/burbuja (nunca a la mitad).

    - Metrica: lineas. Lo que ocupa pantalla y contexto es el texto, no el numero de
      filas de `messages`: una burbuja B larga es UNA fila con cientos de lineas, asi que
      podar por `at`/`keep` dejaria la poda desproporcionada respecto al contenido real.
    - Grupo indivisible: cada burbuja B (id 'b:*') es un grupo; cada mensaje suelto
      (user/legacy/bot no-B) es su propio grupo.
    - Protegidos: turnos vivos (omb_option_b_turns.closed_at IS NULL) y notas de sistema.
      No se borran ni se cuentan como victimas.
    - Nunca se supera el objetivo: se borra el prefijo mas antiguo de grupos mientras su
      suma de lineas quede <= objetivo. Si el grupo siguiente lo excederia, se borra menos.
    - No se toca `omb_deliveries` ni `MIRROR_PREF`: son las tombstones/cursor que impiden
      al mirror resucitar lo depurado. El cursor se queda en el mensaje mas reciente, de
      modo que un reinicio no re-recorre el tramo borrado.
    Devuelve el numero de filas `messages` borradas."""
    pct = PURGE_PCT if pct is None else float(pct)
    pct = min(99.0, max(0.0, pct))
    target_frac = pct / 100.0
    with _dblock, db() as c:
        rows = c.execute("SELECT id,role,content,at FROM messages ORDER BY at,id").fetchall()
        live_bubbles = {r["id"] for r in c.execute(
            "SELECT t.message_id AS id FROM omb_option_b_turns t WHERE t.closed_at IS NULL")}
        groups = []
        total_lines = 0
        for r in rows:
            lines = _content_lines(r["content"])
            total_lines += lines
            protected = (r["role"] == "system") or (r["id"] in live_bubbles)
            groups.append({"id": r["id"], "lines": lines, "protected": protected})
        target = int(total_lines * target_frac)
        victims = []
        removed_lines = 0
        for g in groups:
            if g["protected"]:
                continue
            if removed_lines + g["lines"] > target:
                break
            removed_lines += g["lines"]
            victims.append(g["id"])
        if victims:
            c.executemany("DELETE FROM messages WHERE id=?", [(v,) for v in victims])
        note = ("Depurado: historial reducido a ~%d%% de las lineas (frontera de turnos) y contexto compactado."
                % int(pct))
        c.execute("INSERT INTO messages(id,role,content,at,uid) VALUES(?,?,?,?,?)",
                  (uuid.uuid4().hex, "system", note, now_ms(), None))
    return len(victims)

def debug_worker(pct=None):
    """Dispara la compactacion NATIVA del hilo de OpenMausBot y poda lineas del chat.
    Nunca toca el hilo con un turno en curso (regla del chat: no reiniciar/depurar en vivo)."""
    pct = PURGE_PCT if pct is None else pct
    _debug_busy(True)
    try:
        deadline = time.time() + 900
        while time.time() < deadline:          # esperar a que el bot este ocioso
            b = _omb_busy()
            if b is False:
                break
            time.sleep(4)
        try:
            omb_post(f"/api/bots/{BOT_ID}/compact", {"threadId": THREAD_ID})
            log_line("debug: compact disparado")
        except Exception as e:
            log_line(f"debug: compact fallo: {e}")
        time.sleep(6)                          # deja que arranque el turno de resumen
        t1 = time.time()
        while time.time() - t1 < 420:
            b = _omb_busy()
            if b is False:
                break
            time.sleep(5)
        n = purge_history(pct)
        log_line(f"debug: purged={n} pct={pct}")
    except Exception as e:
        log_line(f"debug: error {e}")
    finally:
        _debug_busy(False)
        _debuging["on"] = False

MIRROR_POLL = 1
MIRROR_PAGE = 200
MIRROR_PENDING_BATCH = 32
MIRROR_PENDING_TTL_MS = 60 * 60 * 1000
MIRROR_BOOTSTRAP_MS = 24 * 60 * 60 * 1000
MIRROR_PREF = "omb_last_message_id"
MIRROR_BOOTSTRAP_PREF = "omb_bootstrap_at"
USER_MIRROR_DEDUP_WINDOW_MS = 5 * 60 * 1000  # ±5 min around OMB `at` to match a PWA-written copy

def _omb_unseen(cursor, cutoff):
    """Read backwards to the persisted cursor, then return messages oldest first."""
    pages = []
    before = None
    newest = None
    while True:
        path = f"/api/threads/{THREAD_ID}/messages?limit={MIRROR_PAGE}"
        if before:
            path += "&before=" + quote(before, safe="")
        response = omb_get(path)
        messages = response["messages"]
        if not isinstance(messages, list) or not isinstance(response.get("hasMore"), bool):
            raise ValueError("OMB pagination response is malformed")
        if messages and newest is None:
            newest = messages[-1]["id"]
        if cursor:
            match = next((i for i, msg in enumerate(messages) if msg["id"] == cursor), None)
            if match is not None:
                pages.append(messages[match + 1:])
                break
        elif messages and int(messages[0].get("at", 0) or 0) < cutoff:
            pages.append([msg for msg in messages if int(msg.get("at", 0) or 0) >= cutoff])
            break
        pages.append(messages)
        if not response["hasMore"]:
            if cursor and not any(msg["id"] == cursor for page in pages for msg in page):
                print("mirror: cursor no longer exists; reconciling available history", flush=True)
            break
        if not messages or messages[0]["id"] == before:
            raise ValueError("OMB pagination failed to advance")
        before = messages[0]["id"]
    return [msg for page in reversed(pages) for msg in page], newest


def _record_b_push(conn, turn_id, phase, body):
    """Registra (exactamente una vez) la decision de push para una fase de un turno B."""
    if conn.execute("SELECT 1 FROM omb_push_ledger WHERE turn_id=? AND phase=?",
                    (turn_id, phase)).fetchone():
        return False
    conn.execute("INSERT INTO omb_push_ledger(turn_id,phase,body,at) VALUES(?,?,?,?)",
                 (turn_id, phase, body, now_ms()))
    return True


def _clean_peer(name):
    """Limita el peer a [A-Za-z0-9 ._-] y un tamano prudente (para mostrarlo)."""
    name = re.sub(r"[^A-Za-z0-9 ._-]", "", (name or "").strip())
    return name[:40] or "Bot"


def _ingest_waiting_activity(message):
    """Ingiere un mensaje kind=='activity' de OMB para seguir la espera de delegaciones.

    - tool.name == 'coordinate_bots' o 'Sent to X' -> espera inicia (registra peer X).
    - '<Peer> replied' -> cierra a ese peer (borra de la lista).
    - turnTerminal -> cierra TODA la espera.
    Tolerante: si nada encaja, no toca el estado (cae al comportamiento actual)."""
    if not OPTION_B:
        return
    if message.get("role") != "bot" or message.get("kind") != "activity":
        return
    tool = message.get("tool")
    name = ""
    if isinstance(tool, dict):
        name = str(tool.get("name") or "")
    out_peers = False
    if message.get("turnTerminal"):
        out_peers = True
    else:
        if name == "coordinate_bots":
            with _waiting_bots_lock:
                if _waiting_bots["since"] is None:
                    _waiting_bots["since"] = now_ms()
                    _waiting_bots["peers"] = []
            return
        m = WAIT_REPLIED_RE.match(name.strip())
        if m:
            peer = _clean_peer(m.group(1))
            with _waiting_bots_lock:
                _waiting_bots["peers"] = [p for p in _waiting_bots["peers"] if p != peer]
                if not _waiting_bots["peers"]:
                    _waiting_bots["since"] = None
            return
        m = WAIT_PEER_RE.match(name.strip())
        if m:
            peer = _clean_peer(m.group(1))
            with _waiting_bots_lock:
                if _waiting_bots["since"] is None:
                    _waiting_bots["since"] = now_ms()
                if peer not in _waiting_bots["peers"]:
                    _waiting_bots["peers"].append(peer)
            return
    if out_peers:
        with _waiting_bots_lock:
            _waiting_bots["since"] = None
            _waiting_bots["peers"] = []


def _waiting_bots_payload():
    """Payload waitingBots para /api/history (None si no espera). Devuelve la
    espera mas antigua (since) con sus peers; la espera se reinicia por cada ventana."""
    with _waiting_bots_lock:
        since = _waiting_bots["since"]
    if since is None:
        return None
    if now_ms() - since > WAIT_STUCK_MS:
        with _waiting_bots_lock:
            _waiting_bots["since"] = None
            _waiting_bots["peers"] = []
        return None
    with _waiting_bots_lock:
        peers = list(dict.fromkeys(_waiting_bots["peers"]))
    return {"since": since, "peers": peers}


def _record_omb_option_b(source_id, body, turn_id, message_at, message):
    """Opcion B: una sola burbuja por turno que se actualiza en silencio."""
    terminal = bool(message.get("turnTerminal"))
    do_start = False
    do_final = False
    final_body = body or None
    with _dblock, db() as conn:
        row = conn.execute("SELECT message_id, final_source_id FROM omb_option_b_turns WHERE turn_id=?",
                           (turn_id,)).fetchone()
        if row is None:
            mid = "b:" + turn_id
            now = now_ms()
            live = 0 if terminal else 1
            conn.execute("INSERT OR IGNORE INTO messages(id,role,content,at,uid,live,subturn) "
                         "VALUES(?,?,?,?,?,?,?)",
                         (mid, "bot", body, now, None, live, turn_id))
            conn.execute("UPDATE messages SET content=?,at=?,live=?,subturn=? WHERE id=?",
                         (body, now, live, turn_id, mid))
            conn.execute("INSERT OR IGNORE INTO omb_option_b_turns(turn_id,message_id,started_at,last_source_id) VALUES(?,?,?,?)",
                         (turn_id, mid, now, source_id))
            conn.execute("UPDATE omb_option_b_turns SET last_source_id=? WHERE turn_id=?",
                         (source_id, turn_id))
            conn.execute("INSERT OR IGNORE INTO omb_deliveries(source_id,message_id) VALUES(?,?)",
                         (source_id, mid))
            do_start = _record_b_push(conn, turn_id, "start", OPTION_B_START_PUSH)
            if terminal:
                conn.execute("UPDATE messages SET content=?,live=0 WHERE id=?", (body, mid))
                conn.execute("UPDATE omb_option_b_turns SET closed_at=?,final_source_id=? WHERE turn_id=?",
                             (now_ms(), source_id, turn_id))
                do_final = _record_b_push(conn, turn_id, "final", body)
        else:
            mid = row["message_id"]
            conn.execute("UPDATE omb_option_b_turns SET last_source_id=? WHERE turn_id=?",
                         (source_id, turn_id))
            conn.execute("INSERT OR IGNORE INTO omb_deliveries(source_id,message_id) VALUES(?,?)",
                         (source_id, mid))
            if terminal and not row["final_source_id"]:
                final_body = body or conn.execute(
                    "SELECT content FROM messages WHERE id=?", (mid,)).fetchone()["content"]
                conn.execute("UPDATE messages SET content=?,at=?,live=0 WHERE id=?",
                             (final_body, now_ms(), mid))
                conn.execute("UPDATE omb_option_b_turns SET closed_at=?,final_source_id=? WHERE turn_id=?",
                             (now_ms(), source_id, turn_id))
                do_final = _record_b_push(conn, turn_id, "final", final_body)
            elif not terminal and not row["final_source_id"]:
                conn.execute("UPDATE messages SET content=?,at=? WHERE id=?", (body, now_ms(), mid))
    if do_start:
        _emit_b_push(OPTION_B_START_PUSH, False)
    if do_final:
        _emit_b_push(final_body, True)


def _emit_b_push(body, force):
    """Envia un push B sin bloquear el poll del mirror (hilo daemon)."""
    def _run():
        try:
            send_push(BRAND, body, force=force)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def _user_text_key(text):
    """Normaliza runas de espacio para comparar copia local vs OMB sin duplicar.
    La PWA usa \n; OMB puede devolver \r\n, dobles espacios o trailing spaces.
    """
    return re.sub(r"\s+", " ", (text or "")).strip()


def _record_omb_user(conn, source_id, body, message_at):
    """Ingiere un role=user/kind=text de OMB.

    Clave de dedup estable = source_id (message["id"] de OMB), guardada en
    omb_user_deliveries. sendId puede variar o faltar entre reenvios: no se usa.

    Orden:
    1. tombstone por source_id -> no-op (idempotente).
    2. exactamente una copia local (uid IS NULL, mismo texto normalizado, ±ventana)
       -> se enlaza con uid=source_id, sin insertar.
    3. cero copias -> se inserta con el `at` real de OMB y uid=source_id.
    4. dos o mas copias -> se inserta tambien la fila de OMB (nunca se pierde);
       no se enlaza ninguna copia local para no adivinar.
    """
    if conn.execute("SELECT 1 FROM omb_user_deliveries WHERE source_id=? LIMIT 1",
                    (source_id,)).fetchone():
        return
    user_at = message_at or now_ms()
    lo = user_at - USER_MIRROR_DEDUP_WINDOW_MS
    hi = user_at + USER_MIRROR_DEDUP_WINDOW_MS
    wanted = _user_text_key(body)
    local_rows = conn.execute(
        "SELECT id, content FROM messages "
        "WHERE role='user' AND uid IS NULL AND at BETWEEN ? AND ? ORDER BY at,id",
        (lo, hi)).fetchall()
    candidates = [row["id"] for row in local_rows if _user_text_key(row["content"]) == wanted]
    if len(candidates) == 1:
        message_id = candidates[0]
        conn.execute("UPDATE messages SET uid=? WHERE id=?", (source_id, message_id))
    else:
        # 0 candidatos: nuevo (UI de OMB sin copia local, o ventana superada).
        # >=2: no adivinamos cual enlazar, pero TAMPOCO perdemos el mensaje OMB.
        message_id = uuid.uuid4().hex
        conn.execute("INSERT INTO messages(id,role,content,at,uid) VALUES(?,?,?,?,?)",
                     (message_id, "user", body, user_at, source_id))
    conn.execute("INSERT OR IGNORE INTO omb_user_deliveries(source_id,message_id) VALUES(?,?)",
                 (source_id, message_id))


def _routine_body(message):
    """Cuerpo legible de un mensaje kind=='routine.run' de OMB.

    OMB pinta la salida de una rutina como tarjeta: `routineRun.summary` y un
    desplegable que la UI nativa recorta. Aqui queremos el informe APLANADO e
    inline, asi que preferimos `text` (trae el cuerpo completo) y caemos a
    `summary` solo si falta. Devuelve None si no hay nada que ingerir.
    """
    rr = message.get("routineRun") or {}
    name = str(rr.get("routineName") or "Rutina").strip() or "Rutina"
    status = str(rr.get("status") or "").strip()
    # Opcion 1 (2026-10-16): la tarjeta de rutina se espeja SOLO como aviso, sin
    # el cuerpo del informe. El informe completo lo publica el mensaje final de
    # Manager (consolidado), de modo que el formato final aparece UNA sola vez.
    # Antes esta funcion traia el texto completo de la ejecucion -> Jose lo veia
    # repetido (tarjeta + borrador del delegado + consolidado).
    head = "**Rutina: %s**" % name
    if status and status != "completed":
        head += " (%s)" % status
    return head


def _record_routine(conn, message, body):
    """Ingiere un routine.run como mensaje normal del chat (idempotente).

    Dedup por source_id en `omb_deliveries`, igual que los mensajes de texto.
    El `at` es el de OMB para que el informe quede en su sitio cronologico.
    `body` se calcula FUERA del lock (ver _record_omb): construirlo hace una
    peticion HTTP a OMB y no debe retener `_dblock`.
    Devuelve True si inserto, False si ya estaba o no habia cuerpo.
    """
    if not body:
        return False
    source_id = message["id"]
    if conn.execute("SELECT 1 FROM omb_deliveries WHERE source_id=? LIMIT 1",
                    (source_id,)).fetchone():
        return False
    message_at = int(message.get("at", 0) or 0)
    mid = uuid.uuid4().hex
    conn.execute("INSERT INTO messages(id,role,content,at,uid) VALUES(?,?,?,?,?)",
                 (mid, "bot", body, message_at if message_at > 0 else now_ms(), None))
    conn.execute("INSERT INTO omb_deliveries(source_id,message_id) VALUES(?,?)",
                 (source_id, mid))
    return True


def _download_omb_attachment(att):
    """Descarga un adjunto que OMB sirve en /api/attachments/<fichero>.

    OMB entrega los adjuntos como {kind, path, mime[, name]} con un `path` DENTRO del
    contenedor de OMB: el host (donde corre este backend) no puede leerlo. Pero
    OMB SI sirve el fichero por HTTP, asi que se descarga de ahi y se guarda como un
    fichero local mas (misma tabla `files` y misma URL /api/file/<id> que los subidos a
    mano), de modo que el visor lo pinta sin cambios. Devuelve (fid,name,mime,size) o None.
    OJO: hace HTTP -> llamar SIEMPRE FUERA de `_dblock`.
    """
    path = (att or {}).get("path") or ""
    base = os.path.basename(path)
    if not base:
        return None
    data = None
    # 1) via HTTP: es la via limpia, pero OMB solo sirve por HTTP los adjuntos de tipo
    #    imagen (comprobado: los .png dan 200 y los .csv 404 "no such attachment").
    try:
        with urlopen(OMB + "/api/attachments/" + quote(base), timeout=30) as r:
            data = r.read()
    except Exception:
        data = None
    # 2) respaldo: leerlo directamente del contenedor de OMB (el servicio corre como un
    #    usuario del grupo docker). Cubre los no-imagen (CSV, TXT, PDF...) que OMB no sirve.
    if data is None and OMB_ATTACH_CONTAINER:
        try:
            out = subprocess.run(
                ["docker", "exec", OMB_ATTACH_CONTAINER, "cat", path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
            if out.returncode == 0 and out.stdout:
                data = out.stdout
        except Exception:
            data = None
    if not data or len(data) > MAX_UPLOAD:
        return None
    fid = uuid.uuid4().hex
    with open(os.path.join(FILES_DIR, fid), "wb") as f:
        f.write(data)
    mime = att.get("mime") or mimetypes.guess_type(base)[0] or "application/octet-stream"
    name = att.get("name") or base
    name = re.sub(r"[^\w.\-() ]", "_", str(name))[:120] or "adjunto"
    return fid, name, mime, len(data)


def _record_omb_attachments(message, source_id, message_at):
    """Espeja en el chat un mensaje de OMB que trae SOLO adjuntos (text vacio).

    Antes se descartaba: `is_text` exige cuerpo y nadie miraba `attachments`, asi que las
    imagenes que Manager adjunta (graficas, capturas) nunca aparecian en la PWA. Ahora se
    descargan, se registran como ficheros locales y se publica un mensaje cuyo cuerpo lleva
    la URL /api/file/<id>: el visor ya sabe pintar imagenes a partir de esa URL.
    Idempotente por source_id. Devuelve True si dejo mensaje.
    """
    if source_id is None:
        return False
    # 1) descargar (HTTP) fuera del lock
    got = []
    for att in (message.get("attachments") or []):
        item = _download_omb_attachment(att)
        if item:
            got.append(item)
    if not got:
        return False
    with _dblock, db() as conn:
        if conn.execute("SELECT 1 FROM omb_deliveries WHERE source_id=? LIMIT 1",
                        (source_id,)).fetchone():
            return False
        links = []
        for fid, name, mime, size in got:
            conn.execute("INSERT OR IGNORE INTO files(id,name,mime,size,created) VALUES(?,?,?,?,?)",
                         (fid, name, mime, size, now_ms()))
            links.append("/api/file/" + fid)
        mid = uuid.uuid4().hex
        conn.execute("INSERT INTO messages(id,role,content,at,uid) VALUES(?,?,?,?,?)",
                     (mid, "bot", "\n".join(links), message_at if message_at > 0 else now_ms(), None))
        conn.execute("INSERT INTO omb_deliveries(source_id,message_id) VALUES(?,?)",
                     (source_id, mid))
        conn.execute("INSERT OR REPLACE INTO prefs(k,v) VALUES(?,?)", (MIRROR_PREF, source_id))
    return True


def _record_omb(message, legacy_since, legacy_until):
    _ingest_waiting_activity(message)
    source_id = message["id"]
    body = (message.get("text") or "").strip()
    role = message.get("role")
    kind = message.get("kind")
    is_text = role == "bot" and kind == "text" and bool(body)
    turn_id = message.get("turnId")
    turn_id = turn_id if isinstance(turn_id, str) and turn_id.strip() else None
    message_at = int(message.get("at", 0) or 0)
    if role == "user" and kind == "text" and bool(body):
        with _dblock, db() as conn:
            _record_omb_user(conn, source_id, body, message_at)
            conn.execute("INSERT OR REPLACE INTO prefs(k,v) VALUES(?,?)", (MIRROR_PREF, source_id))
        return
    if role == "bot" and kind == "routine.run":
        # H1: el cuerpo (con su HTTP a OMB) se construye ANTES del lock.
        # H2: el cursor solo avanza si de verdad se inserto el informe.
        routine_body = _routine_body(message)
        with _dblock, db() as conn:
            inserted = _record_routine(conn, message, routine_body)
            if inserted:
                conn.execute("INSERT OR REPLACE INTO prefs(k,v) VALUES(?,?)", (MIRROR_PREF, source_id))
        return
    if role == "bot" and kind == "text" and message.get("attachments"):
        # Adjuntos sin texto (graficas, capturas, CSV): espejarlos como ficheros locales.
        _record_omb_attachments(message, source_id, message_at)
        return
    if role == "bot" and kind == "text" and isinstance(message.get("from"), dict):
        # Opcion 1 (2026-10-16): el borrador del delegado (respuesta de otro bot,
        # p.ej. "@Analista replied to the delegated task: ...") NO se espeja; el
        # mensaje final de Manager ya consolida su contenido. Solo se avanza el
        # cursor para no re-leerlo. Evita el informe repetido.
        with _dblock, db() as conn:
            conn.execute("INSERT OR REPLACE INTO prefs(k,v) VALUES(?,?)", (MIRROR_PREF, source_id))
        return
    if OPTION_B and is_text and turn_id:
        _record_omb_option_b(source_id, body, turn_id, message_at, message)
        with _dblock, db() as conn:
            conn.execute("INSERT OR REPLACE INTO prefs(k,v) VALUES(?,?)", (MIRROR_PREF, source_id))
        return
    with _dblock, db() as conn:
        if is_text:
            seen = conn.execute("SELECT 1 FROM omb_deliveries WHERE source_id=?", (source_id,)).fetchone()
            inserted = False
            if not seen:
                old = None
                if legacy_since <= message_at <= legacy_until:
                    old = conn.execute("""SELECT m.id FROM messages m
                        LEFT JOIN omb_deliveries d ON d.message_id=m.id
                        WHERE m.role='bot' AND m.content=? AND m.at BETWEEN ? AND ?
                          AND d.source_id IS NULL
                        ORDER BY m.at, m.id LIMIT 1""",
                        (body, legacy_since, legacy_until)).fetchone()
                message_id = old["id"] if old else uuid.uuid4().hex
                if not old:
                    conn.execute("INSERT INTO messages(id,role,content,at,uid) VALUES(?,?,?,?,?)",
                                 (message_id, "bot", body, now_ms(), None))
                    inserted = True
                conn.execute("INSERT INTO omb_deliveries(source_id,message_id) VALUES(?,?)",
                             (source_id, message_id))
            if turn_id and not conn.execute(
                    "SELECT 1 FROM omb_push_events WHERE turn_id=?", (turn_id,)).fetchone():
                if message.get("turnTerminal"):
                    pending = conn.execute("""SELECT push_eligible FROM omb_pending_turns
                        WHERE turn_id=? AND source_id=?""", (turn_id, source_id)).fetchone()
                    conn.execute("""INSERT OR IGNORE INTO omb_push_events
                        (turn_id,source_id,message_at,attempted_at) VALUES(?,?,?,?)""",
                        (turn_id, source_id, message_at,
                         None if inserted or pending and pending["push_eligible"] else now_ms()))
                    conn.execute("DELETE FROM omb_pending_turns WHERE turn_id=?", (turn_id,))
                else:
                    conn.execute("""INSERT INTO omb_pending_turns
                        (turn_id,source_id,message_at,push_eligible,next_check_at)
                        VALUES(?,?,?,?,0) ON CONFLICT(turn_id) DO UPDATE SET
                        source_id=excluded.source_id, message_at=excluded.message_at,
                        push_eligible=CASE WHEN omb_pending_turns.source_id=excluded.source_id
                            THEN omb_pending_turns.push_eligible ELSE excluded.push_eligible END,
                        next_check_at=0""",
                        (turn_id, source_id, message_at, int(inserted)))
        conn.execute("INSERT OR REPLACE INTO prefs(k,v) VALUES(?,?)", (MIRROR_PREF, source_id))

def _check_pending_turns():
    with db() as conn:
        pending = conn.execute("""SELECT turn_id,source_id,message_at,push_eligible FROM omb_pending_turns
            WHERE next_check_at<=? ORDER BY next_check_at,turn_id LIMIT ?""",
            (now_ms(), MIRROR_PENDING_BATCH)).fetchall()
    for row in pending:
        if now_ms() - row["message_at"] > MIRROR_PENDING_TTL_MS:
            with _dblock, db() as conn:
                removed = conn.execute("DELETE FROM omb_pending_turns WHERE turn_id=? AND source_id=?",
                                       (row["turn_id"], row["source_id"])).rowcount
            if removed:
                print("mirror: expired pending turn after one hour without terminal", flush=True)
            continue
        try:
            response = omb_get(f"/api/threads/{THREAD_ID}/messages?limit=3&around=" + quote(row["source_id"], safe=""))
        except HTTPError as exc:
            if exc.code != 404:
                raise
            with _dblock, db() as conn:
                conn.execute("UPDATE omb_pending_turns SET next_check_at=? WHERE turn_id=? AND source_id=?",
                             (now_ms() + 60_000, row["turn_id"], row["source_id"]))
            continue
        message = next((item for item in response["messages"] if item["id"] == row["source_id"]), None)
        if message is None:
            raise ValueError("OMB around response omitted pending message")
        if message.get("turnId") != row["turn_id"] or message.get("role") != "bot" or message.get("kind") != "text":
            raise ValueError("OMB pending message changed identity")
        with _dblock, db() as conn:
            current = conn.execute("SELECT source_id FROM omb_pending_turns WHERE turn_id=?",
                                   (row["turn_id"],)).fetchone()
            if not current or current["source_id"] != row["source_id"]:
                continue
            if message.get("turnTerminal"):
                conn.execute("""INSERT OR IGNORE INTO omb_push_events
                    (turn_id,source_id,message_at,attempted_at) VALUES(?,?,?,?)""",
                    (row["turn_id"], row["source_id"], row["message_at"], None))
                conn.execute("DELETE FROM omb_pending_turns WHERE turn_id=?", (row["turn_id"],))
            else:
                delay = 60_000 if now_ms() - row["message_at"] >= 300_000 else MIRROR_POLL * 1000
                conn.execute("UPDATE omb_pending_turns SET next_check_at=? WHERE turn_id=?",
                             (now_ms() + delay, row["turn_id"]))

def _check_option_b_open_turns():
    """H9: re-chequeo de marca tardia para la via B.

    Un turno B abierto (closed_at IS NULL) que llego sin turnTerminal en el primer
    avance se re-consulta con GET /messages?limit=3&around=<last_source_id>. Si OMB ya
    puso turnTerminal=true, cierra la burbuja (live=0, content final, closed_at,
    final_source_id) y registra el push final UNA vez via omb_push_ledger. Si sigue sin
    cerrar, reprograma. A la hora de no cerrar, lo abandona y lo loguea.
    """
    if not OPTION_B:
        return
    with db() as conn:
        rows = conn.execute("""SELECT turn_id,message_id,started_at,last_source_id,next_check_at
                                FROM omb_option_b_turns
                                WHERE closed_at IS NULL AND next_check_at<=?
                                ORDER BY next_check_at,turn_id LIMIT ?""",
                            (now_ms(), MIRROR_PENDING_BATCH)).fetchall()
    rows = [dict(r) for r in rows]
    for row in rows:
        turn_id = row["turn_id"]
        source_id = row["last_source_id"]
        started_at = int(row["started_at"])
        # H9 (cabo 2): abandono por TTL cierra el turno y quita live=1 (sin push).
        if not source_id or now_ms() - started_at > MIRROR_PENDING_TTL_MS:
            with _dblock, db() as conn:
                cur = conn.execute("SELECT closed_at FROM omb_option_b_turns WHERE turn_id=?",
                                   (turn_id,)).fetchone()
                if not cur or cur["closed_at"] is not None:
                    continue
                changed = conn.execute("UPDATE omb_option_b_turns SET closed_at=? WHERE turn_id=?",
                                       (now_ms(), turn_id)).rowcount
                conn.execute("UPDATE messages SET live=0 WHERE id=?", (row["message_id"],))
            if changed:
                reason = "sin source_id" if not source_id else "transcurrida una hora"
                print(f"mirror: option B turn left open ({reason})", flush=True)
            continue
        try:
            response = omb_get("/api/threads/" + THREAD_ID + "/messages?limit=3&around=" + quote(source_id, safe=""))
        except Exception:
            # programar reintento corto
            with _dblock, db() as conn:
                conn.execute("UPDATE omb_option_b_turns SET next_check_at=? WHERE turn_id=?",
                             (now_ms() + 60_000, turn_id))
            continue
        message = next((item for item in response.get("messages", []) if item.get("id") == source_id), None)
        if message is None:
            continue
        if message.get("turnId") != turn_id:
            continue
        if message.get("turnTerminal"):
            with _dblock, db() as conn:
                cur = conn.execute("SELECT closed_at FROM omb_option_b_turns WHERE turn_id=?",
                                   (turn_id,)).fetchone()
                if not cur or cur["closed_at"] is not None:
                    continue
                final_body = str(message.get("text") or "").strip() or conn.execute(
                    "SELECT content FROM messages WHERE id=?", (row["message_id"],)).fetchone()["content"]
                conn.execute("UPDATE messages SET content=?,at=?,live=0 WHERE id=?",
                             (final_body, now_ms(), row["message_id"]))
                conn.execute("UPDATE omb_option_b_turns SET closed_at=?,final_source_id=? WHERE turn_id=?",
                             (now_ms(), source_id, turn_id))
                final = _record_b_push(conn, turn_id, "final", final_body)
            if final:
                _emit_b_push(final_body, True)
        else:
            delay = 60_000 if now_ms() - int(row["started_at"]) >= 300_000 else MIRROR_POLL * 1000
            with _dblock, db() as conn:
                conn.execute("UPDATE omb_option_b_turns SET next_check_at=? WHERE turn_id=?",
                             (now_ms() + delay, turn_id))

PUSH_MAX_ATTEMPTS = 3
PUSH_RETRY_MS = 1000
PUSH_LEASE_MS = 60_000
_push_inflight = set()

def _finish_push_event(event, outcome):
    exhausted = False
    with _dblock:
        try:
            with db() as conn:
                if outcome is not False or event["attempts"] >= PUSH_MAX_ATTEMPTS:
                    changed = conn.execute("""UPDATE omb_push_events SET attempted_at=?,next_attempt_at=0
                        WHERE turn_id=? AND attempted_at IS NULL AND attempts=?""",
                        (now_ms(), event["turn_id"], event["attempts"])).rowcount
                    exhausted = bool(changed and outcome is False)
                else:
                    conn.execute("""UPDATE omb_push_events SET next_attempt_at=?
                        WHERE turn_id=? AND attempted_at IS NULL AND attempts=?""",
                        (now_ms() + PUSH_RETRY_MS * 2 ** (event["attempts"] - 1),
                         event["turn_id"], event["attempts"]))
        finally:
            _push_inflight.discard(event["turn_id"])
    if exhausted:
        print(f"mirror: push delivery exhausted after {PUSH_MAX_ATTEMPTS} attempts", flush=True)

def _deliver_push_event(event):
    try:
        outcome = send_push(BRAND, event["content"])
    except Exception:
        outcome = False
    _finish_push_event(event, outcome)

def _dispatch_push(event):
    threading.Thread(target=_deliver_push_event, args=(event,), daemon=True).start()

def _drain_push_events():
    push_enabled = get_pref("push_enabled", "true") != "false"
    with _dblock, db() as conn:
        current = now_ms()
        events = conn.execute("""SELECT e.turn_id,e.message_at,e.attempts,m.content FROM omb_push_events e
            JOIN omb_deliveries d ON d.source_id=e.source_id
            JOIN messages m ON m.id=d.message_id
            WHERE e.attempted_at IS NULL AND e.next_attempt_at<=?
            ORDER BY e.rowid LIMIT 50""", (current,)).fetchall()
        ready = []
        for event in events:
            if event["turn_id"] in _push_inflight:
                continue
            if event["attempts"] >= PUSH_MAX_ATTEMPTS:
                conn.execute("UPDATE omb_push_events SET attempted_at=? WHERE turn_id=? AND attempted_at IS NULL",
                             (current, event["turn_id"]))
                print(f"mirror: push delivery exhausted after {PUSH_MAX_ATTEMPTS} attempts", flush=True)
            elif not push_enabled or (event["attempts"] == 0 and not -5_000 <= current - event["message_at"] < 300_000):
                conn.execute("UPDATE omb_push_events SET attempted_at=? WHERE turn_id=? AND attempted_at IS NULL",
                             (current, event["turn_id"]))
            else:
                attempt = event["attempts"] + 1
                conn.execute("""UPDATE omb_push_events SET attempts=?,next_attempt_at=?
                    WHERE turn_id=? AND attempted_at IS NULL""",
                    (attempt, current + PUSH_LEASE_MS, event["turn_id"]))
                ready.append(dict(event) | {"attempts": attempt})
                _push_inflight.add(event["turn_id"])
    for event in ready:
        try:
            _dispatch_push(event)
        except Exception:
            _finish_push_event(event, False)

def _mirror_cursor():
    with db() as conn:
        row = conn.execute("SELECT v FROM prefs WHERE k=?", (MIRROR_PREF,)).fetchone()
    return row["v"] if row else None

def _bootstrap_at():
    with _dblock, db() as conn:
        row = conn.execute("SELECT v FROM prefs WHERE k=?", (MIRROR_BOOTSTRAP_PREF,)).fetchone()
        if row:
            return int(row["v"])
        started = now_ms()
        conn.execute("INSERT INTO prefs(k,v) VALUES(?,?)", (MIRROR_BOOTSTRAP_PREF, str(started)))
        return started

def _mirror_follow():
    """Continuously reconcile OMB, independent of dispatch and agent turn state."""
    bootstrap_at = _bootstrap_at()
    cutoff = bootstrap_at - MIRROR_BOOTSTRAP_MS
    while True:
        try:
            cursor = _mirror_cursor()
            messages, newest = _omb_unseen(cursor, cutoff)
            for message in messages:
                _record_omb(message, cutoff, bootstrap_at)
            if cursor is None and not messages and newest:
                set_pref(MIRROR_PREF, newest)
        except Exception as exc:
            print(f"mirror: reconciliation failed: {type(exc).__name__}: {exc}", flush=True)
        try:
            _check_pending_turns()
        except Exception as exc:
            print(f"mirror: pending turn check failed: {type(exc).__name__}", flush=True)
        try:
            _check_option_b_open_turns()
        except Exception as exc:
            print(f"mirror: option B open turn check failed: {type(exc).__name__}", flush=True)
        try:
            _drain_push_events()
        except Exception as exc:
            print(f"mirror: push event check failed: {type(exc).__name__}", flush=True)
        time.sleep(MIRROR_POLL)

def send_push(title, body, force=False):
    if not (_HAS_PUSH and VAPID.get("private_key")):
        return None
    # Si la UI del chat estuvo en primer plano hace poco (poll reciente de /api/history),
    # Jose esta leyendo: no duplicamos con una notificacion. Al minimizar, iOS congela la
    # pagina, cesan los polls y a los ~25s vuelven los avisos. (force=para pruebas/push manual)
    if not force and (now_ms() - _viewer["last"]) < VIEWER_TTL_MS:
        return None
    # Precisa (Opcion B): aunque force=True (push FINAL), si la app esta visible, pegada al
    # final y hubo un POST /api/view reciente, suprimimos igual. Si nunca llega /api/view
    # (viewer_at queda en 0), la condicion no aplica y degradamos al comportamiento de A.
    if (_viewer.get("visible") is True and _viewer.get("at_bottom") is True
            and (now_ms() - (_viewer.get("viewer_at") or 0)) < VIEWER_TTL_MS):
        return None
    try:
        with db() as c:
            subs = [r["sub"] for r in c.execute("SELECT sub FROM push_subs").fetchall()]
    except Exception:
        return False
    if not subs:
        return None
    payload = json.dumps({"title": title, "body": body or ""})
    failed = False
    for sub in subs:
        try:
            info = json.loads(sub)
            webpush(subscription_info=info, data=payload,
                    vapid_private_key=VAPID["private_key"],
                    vapid_claims={"sub": "mailto:manager@merkle.space"})
        except WebPushException as error:
            code = getattr(getattr(error, "response", None), "status_code", None)
            if code in (404, 410):
                try:
                    with _dblock, db() as c:
                        c.execute("DELETE FROM push_subs WHERE sub=?", (sub,))
                except Exception:
                    failed = True
            else:
                failed = True
        except Exception:
            failed = True
    return not failed

def _omb_steer(text, interrupt=False):
    """Envia un texto de usuario al turno VIVO de OMB usando su endpoint nativo.

    - interrupt=False: POST /api/bots/{id}/messages {text,threadId} -> OMB pliega/encola
      el texto en el turno vivo (steer nativo) SIN pasar por _dispatch_lock.
    - interrupt=True: ademas llama POST /api/bots/{id}/interrupt {threadId} para
      descontinuar el turno (semantica distinta de un steer normal).
    Devuelve (steer_ok, interrupt_ok)."""
    steer_ok = True
    try:
        omb_post(f"/api/bots/{BOT_ID}/messages", {"text": text, "threadId": THREAD_ID})
    except Exception:
        steer_ok = False
    interrupt_ok = None
    if interrupt:
        try:
            omb_post(f"/api/bots/{BOT_ID}/interrupt", {"threadId": THREAD_ID})
            interrupt_ok = True
        except Exception:
            interrupt_ok = False
    return steer_ok, interrupt_ok


def _mark_busy(on):
    """Ajusta el flag local de trabajo sin dejarlo en un contador negativo."""
    if on:
        _busy["q"] += 1
        _busy["on"] = True
        if not _busy["since"]:
            _busy["since"] = now_ms()
    else:
        _busy["q"] = max(0, _busy["q"] - 1)
        if _busy["q"] == 0:
            _busy["on"] = False
            _busy["since"] = 0


def _busy_since_ms(busy):
    """H10: marca coherente para el contador 'trabajando (Ns)'.

    Con busy=true devuelve el inicio real del TURNO EN CURSO: la burbuja B abierta mas
    reciente (omb_option_b_turns.started_at, MAX), o el ancla local _busy["since"] (turno
    via A / dispatch nuevo, fijado y reseteado por turno). Elegimos MAX y no MIN: si queda
    un turno residual abierto antiguo, no debe inflar el contador; la marca debe reflejar
    el turno en curso mas reciente. Si no conocemos un inicio fiable devolvemos 0, de modo
    que el cliente ancle al primer poll que vio busy=true y reinicie al volver a true tras
    un false. La depuracion (debug_worker) ya NO fija _busy["since"], asi que no puede
    arrastrar su tramo de espera/compactacion.
    Con busy=false devolvemos el flag local (0 si no hay): el contador se oculta."""
    if busy:
        if TURN_CONTROL:
            try:
                with db() as c:
                    row = c.execute("SELECT MAX(started_at) AS s FROM omb_option_b_turns WHERE closed_at IS NULL").fetchone()
                if row and row["s"]:
                    return int(row["s"])
            except Exception:
                pass
        if _busy["since"]:
            return int(_busy["since"])
        # Sin marca fiable del turno en curso: el cliente ancla por tarea.
        return 0
    return int(_busy["since"] or 0)


def _omb_busy_bool():
    """F2 + H7: indicador 'trabajando' sin bloquear /api/history.

    Cache TTL corto: si la cache esta fresca, devuelve su valor. Si expiro, arranca un
    refresh en background (timeout <=3s) y devuelve ya el valor cacheado o el fallback
    local. Nunca espera 30 s en el hilo del handler."""
    now = now_ms()
    with _busy_cache_lock:
        if _busy_cache["value"] is not None and now - _busy_cache["at"] < BUSY_CACHE_TTL_MS:
            return bool(_busy_cache["value"])
        if _busy_cache["refreshing"]:
            stuck = now - _busy_cache.get("refreshing_since", 0) > BUSY_REFRESH_STUCK_MS
            if stuck:
                # H8: el hilo anterior murio sin su finally; liberamos la marca para
                # permitir un refresco nuevo (no devolvemos el valor viejo para siempre).
                _busy_cache["refreshing"] = False
                _busy_cache["refreshing_since"] = 0
            else:
                return bool(_busy_cache["value"]) if _busy_cache["value"] is not None else bool(_busy["on"])
        _busy_cache["refreshing"] = True
        _busy_cache["refreshing_since"] = now

    def _refresh():
        try:
            value = _omb_busy_fetch()
            with _busy_cache_lock:
                _busy_cache["value"] = value
                _busy_cache["at"] = now_ms()
                _busy_cache["refreshing_since"] = 0
        finally:
            with _busy_cache_lock:
                _busy_cache["refreshing"] = False

    threading.Thread(target=_refresh, daemon=True).start()
    with _busy_cache_lock:
        return bool(_busy_cache["value"]) if _busy_cache["value"] is not None else bool(_busy["on"])


def dispatch(text, uid=None, interrupt=False):
    """F1: guarda el mensaje del usuario ANTES de cualquier candado (via activa)."""
    if not TURN_CONTROL:
        with _dispatch_lock:
            _mark_busy(True)
            try:
                _dispatch_locked(text, uid)
            finally:
                _mark_busy(False)
        return

    add_msg("user", text, uid)
    busy = _omb_busy()
    # Interrupcion (F4): solo aplica cuando HAY turno vivo. Con bot ocioso el texto se
    # trata como un turno normal para que el agente lo lea igualmente.
    if interrupt and busy is True:
        steer_ok, interrupt_ok = _omb_steer(text, interrupt=True)
        if steer_ok and interrupt_ok is True:
            add_msg("system", "Deteniendo el turno de Manager...")
        return
    # Steer normal (F4): mensaje nuevo con turno en curso, SIN esperar el candado.
    if busy is True:
        steer_ok, _ = _omb_steer(text, interrupt=False)
        if not steer_ok:
            add_msg("system", "No se pudo enviar el mensaje en curso; intentalo de nuevo.")
        return
    # Bot libre: disparar un turno nuevo (F2: sin rendirse a los 600 s).
    with _dispatch_lock:
        _mark_busy(True)
        try:
            _dispatch_locked_persisted(text)
        finally:
            _mark_busy(False)

def _dispatch_locked(text, uid=None):
    """Via A original: add_msg dentro del candado + espera con umbral de 600 s."""
    add_msg("user", text, uid)
    try:
        omb_post(f"/api/bots/{BOT_ID}/messages", {"text": text, "threadId": THREAD_ID})
    except Exception as e:
        add_msg("system", f"No se pudo disparar el turno: {e}")
        return
    deadline = time.time() + 600
    saw_busy = False
    unknown_since = None
    while time.time() < deadline:
        time.sleep(2)
        busy = _omb_busy()
        if busy is None:
            if unknown_since is None:
                unknown_since = time.monotonic()
            elif time.monotonic() - unknown_since >= 30:
                add_msg("system", "No se pudo verificar el estado de Manager. El mensaje se envió; la respuesta seguirá sincronizándose.")
                return
            continue
        unknown_since = None
        saw_busy = saw_busy or busy is True
        if busy is False and (saw_busy or time.time() >= deadline - 596):
            return
    add_msg("system", "Manager no respondió a tiempo. El mensaje está guardado; la respuesta seguirá sincronizándose.")


def _dispatch_locked_persisted(text):
    """F2: dispara el turno SIN rendirse a los 600 s y sin duplicar add_msg.

    El mensaje del usuario ya esta guardado por dispatch() (F1). Aqui solo se hace el
    POST a OMB; la finalizacion la detecta el mirror, no este hilo. El indicador
    'trabajando' lo entrega /api/history leyendo _omb_busy_bool()."""
    try:
        omb_post(f"/api/bots/{BOT_ID}/messages", {"text": text, "threadId": THREAD_ID})
    except Exception as e:
        add_msg("system", f"No se pudo disparar el turno: {e}")

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
            if p == "/manifest.webmanifest":
                return self._manifest()
            if p in ("/sw.js", "/theme.css", "/app.js", "/icon.svg", "/icon-512.png", "/apple-touch-icon.png"):
                return self._static(p.lstrip("/"))
            if p == "/api/me":
                # NV2: el correo del dueno solo sale con sesion. Sin sesion no se
                # devuelve, para que nadie pueda leerlo desde fuera.
                ok = self._session_ok()
                datos = {"ok": ok, "brand": BRAND,
                         "turnControl": bool(TURN_CONTROL),
                         "search": bool(SEARCH_ON), "searchMin": SEARCH_MIN}
                if ok:
                    datos["email"] = OWNER
                return self._json(200, datos)
            if p == "/api/push/key":
                if not self._require():
                    return
                return self._json(200, {"key": VAPID.get("public_key")})
            if p == "/api/push/prefs":
                if not self._require():
                    return
                return self._json(200, {"enabled": get_pref("push_enabled", "true") != "false"})
            if p == "/api/history":
                if not self._require():
                    return
                q = parse_qs(u.query)
                before = q.get("before", [None])[0]
                after = q.get("after", [None])[0]
                lim = min(int(q.get("limit", ["60"])[0] or 60), 200)
                _viewer["last"] = now_ms()
                busy = _omb_busy_bool() if TURN_CONTROL else bool(_busy["on"])
                busy_since = _busy_since_ms(busy)
                waiting_bots = _waiting_bots_payload() if OPTION_B else None
                return self._json(200, {"messages": history(lim, before, after),
                                         "busy": busy,
                                         "busySince": busy_since,
                                         "waitingBots": waiting_bots})
            if p == "/api/search":
                if not self._require():
                    return
                return self._search(u)
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
            if p == "/api/view":
                if not self._require():
                    return
                return self._view()
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
                threading.Thread(target=send_push, args=(BRAND, "Prueba de notificacion"), kwargs={"force": True}, daemon=True).start()
                return self._json(200, {"ok": True})
            if p == "/api/push/prefs":
                if not self._require():
                    return
                return self._push_prefs_set()
            if p == "/api/interrupt":
                if not self._require():
                    return
                return self._interrupt()
            if p == "/api/send":
                if not self._require():
                    return
                return self._send()
            if p == "/api/debug":
                if not self._require():
                    return
                return self._debug()
            return self._json(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"error": str(e)})

    # ---- static ----
    def _manifest(self):
        path = os.path.join(STATIC_DIR, "manifest.webmanifest")
        try:
            with open(path, encoding="utf-8") as f:
                man = json.load(f)
        except Exception:
            man = {}
        name = os.environ.get("CHAT_PWA_NAME", "").strip()
        if name:
            man["name"] = name
        man["short_name"] = BRAND
        return self._json(200, man)

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
        # NV1: solo se confia en cabeceras si la conexion viene del proxy local.
        # Este backend escucha solo en 127.0.0.1, asi que un peer loopback es Caddy,
        # que sobrescribe X-Forwarded-For con {remote_host}. cf-connecting-ip solo
        # se cree con CHAT_TRUST_CF=1 (despliegue real detras de Cloudflare).
        peer = self.client_address[0]
        if peer in ("127.0.0.1", "::1"):
            if os.environ.get("CHAT_TRUST_CF") == "1":
                cf = (self.headers.get("cf-connecting-ip") or "").strip()
                if cf:
                    return cf
            xff = (self.headers.get("x-forwarded-for") or "").split(",")[-1].strip()
            if xff:
                return xff
        return peer

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
        # F1/NV2 (S-2026-11-15): el cliente ya no manda ningun correo y el servidor
        # no lo lee: siempre se manda al dueno configurado. Asi no hay comparacion
        # de email que sondear (ni por respuesta ni por latencia) y no hace falta
        # que el navegador conozca la direccion. Toda la logica del dueno va en un
        # hilo daemon y el 200 se escribe antes de lanzarlo.
        try:
            self._body()
        except Exception:
            pass
        self._json(200, {"ok": True})
        threading.Thread(target=self._issue_token_for_owner, daemon=True).start()

    def _issue_token_for_owner(self):
        # F2 (S-2026-11-15): el tope global se comprueba dentro del hilo, antes de
        # generar o insertar el token. Si esta agotado ya no hay canal de respuesta:
        # solo se deja constancia en mail.log y el HTTP ya habia devuelto 200.
        if not self._rate_ok("token_gen_global", TOKEN_GEN_LIMIT_PER_HOUR, 3600):
            log_line("TOKEN LIMITE GLOBAL alcanzado, no se genero token")
            return
        tok = secrets.token_urlsafe(18)
        with _dblock, db() as c:
            c.execute("INSERT INTO logins(token,created,expires,used) VALUES(?,?,?,0)",
                      (tok, int(time.time()), int(time.time()) + TOKEN_MIN * 60))
        # El envio SMTP es asincrono y los fallos quedan en mail.log mediante
        # _send_token_safely; nunca forman parte de la respuesta HTTP.
        _send_token_safely(OWNER, tok)

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
    def _debug(self):
        if _debuging["on"]:
            return self._json(200, {"ok": True, "started": False, "reason": "running"})
        _debuging["on"] = True
        threading.Thread(target=debug_worker, daemon=True).start()
        return self._json(200, {"ok": True, "started": True})

    def _interrupt(self):
        """F4: Detener llama a POST /api/bots/{id}/interrupt con el threadId correcto."""
        if not TURN_CONTROL:
            return self._json(200, {"ok": True, "reason": "disabled"})
        steer_ok, interrupt_ok = _omb_steer(DETENER_DEFAULT, interrupt=True)
        if interrupt_ok is True:
            add_msg("system", "Deteniendo el turno de Manager...")
            return self._json(200, {"ok": True})
        return self._json(502, {"ok": False, "error": "interrupt-failed"})

    def _send(self):
        body = json.loads(self._body().decode("utf-8") or "{}")
        text = (body.get("text") or "").strip()
        atts = body.get("attachments") or []
        interrupt = bool(body.get("interrupt") or (TURN_CONTROL and _is_stop_text(text)))
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
        threading.Thread(target=dispatch, args=(payload,), kwargs={"interrupt": interrupt}, daemon=True).start()
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

    def _push_prefs_set(self):
        body = json.loads(self._body().decode("utf-8") or "{}")
        if "enabled" in body:
            set_pref("push_enabled", "true" if body.get("enabled") else "false")
        return self._json(200, {"enabled": get_pref("push_enabled", "true") != "false"})

    def _push_unsubscribe(self):
        body = json.loads(self._body().decode("utf-8") or "{}")
        endpoint = body.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            return self._json(400, {"error": "bad endpoint"})
        with _dblock, db() as c:
            c.execute("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))
        return self._json(200, {"ok": True})

    def _search(self, u):
        """GET /api/search?q=...&wiki=1 -> coincidencias en el chat (no depurado) y, si
        wiki=1, tambien en la wiki de memoria. Sin minimo de letras aqui: el front exige
        SEARCH_MIN; el backend solo rechaza la consulta vacia."""
        if not SEARCH_ON:
            return self._json(200, {"enabled": False, "chat": [], "wiki": []})
        q = parse_qs(u.query)
        term = (q.get("q", [""])[0] or "").strip()
        want_wiki = (q.get("wiki", ["0"])[0] or "0").strip().lower() in ("1", "true", "yes", "on")
        if not term:
            return self._json(200, {"enabled": True, "q": "", "chat": [], "wiki": []})
        chat_hits = search_chat(term)
        wiki_hits = search_wiki(term) if want_wiki else []
        return self._json(200, {
            "enabled": True, "q": term,
            "chat": chat_hits, "wiki": wiki_hits,
            "chatCount": len(chat_hits),
            "wikiCount": sum(h["count"] for h in wiki_hits),
        })

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
        # Solo las imagenes se sirven inline (para verlas en la burbuja). Cualquier otro
        # fichero (csv, pdf, docx, txt...) va como `attachment` para que el navegador lo
        # DESCARGUE a Archivos del telefono en vez de abrirlo en un visor sin boton de cerrar
        # (peticion de Jose, 2026-10-19).
        inline = r["mime"].startswith("image/")
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

    def _view(self):
        body = json.loads(self._body().decode("utf-8") or "{}")
        visible = bool(body.get("visible"))
        at_bottom = bool(body.get("atBottom"))
        _viewer["visible"] = visible
        _viewer["at_bottom"] = at_bottom
        _viewer["viewer_at"] = now_ms()
        return self._json(200, {"ok": True})

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

if __name__ == "__main__":
    init_db()
    threading.Thread(target=_mirror_follow, daemon=True, name="omb-mirror").start()
    if SEARCH_ON:
        threading.Thread(target=_wiki_prewarm, daemon=True, name="chat-wiki-warm").start()
    print(f"chat backend on 127.0.0.1:{PORT}  data={DATA_DIR}", flush=True)
    Server(("127.0.0.1", PORT), Handler).serve_forever()
