# Agent-Chat

Un **chat privado tipo PWA** entre **una persona y su agente/bot**, pensado para
instalarse en el móvil (pantalla de inicio) y recibir notificaciones push reales
con la app cerrada.

Un solo fichero de backend en Python (librería estándar, sin frameworks), un
frontend PWA estático y **Web Push con VAPID**. Sin dependencias de terceros
para el chat en sí; solo `pywebpush` para las notificaciones.

![icono](static/icon-512.png)

---

## Qué hace

- **Chat de texto** con burbujas, historial propio y adjuntos (imágenes y ficheros, hasta 25 MB).
- **Formato ligero en los mensajes**: `**negritas**`, `` `código` ``, *cursiva*, listas
  (con guion o numeradas), citas, títulos, separadores y **tablas markdown** (con
  alineación por columnas `:---`/`:---:`/`---:` y scroll horizontal en móvil). Se
  renderiza sobre el texto ya escapado, así que el contenido nunca puede inyectar HTML.
- **Autenticación por código de un solo uso** enviado por email (sin contraseñas).
- **Sesión de 30 días** en una cookie `HttpOnly`, `Secure`, `SameSite=Lax`.
- **PWA instalable** en iOS/Android (Safari → *Añadir a pantalla de inicio*).
- **Notificaciones push** (sonido, app cerrada) mediante VAPID + service worker.
- **Botón "Depurar"**: compacta el contexto del hilo con la compactación nativa de OpenMausBot
  y reduce el historial del chat al mínimo, para que el agente vuelva a responder rápido.
  Respeta los turnos en curso (nunca depura en vivo).
- **Acuse rápido + respuesta final**: en cuanto el agente empieza a responder se publica
  un primer mensaje corto, y al terminar llega la respuesta completa. Nunca te deja colgado.
- **Indicador "está trabajando… (Ns)"** durante todo el turno, con los segundos que lleva.
- **Un turno a la vez**: los mensajes se serializan, y una respuesta nunca se publica dos veces.
- **El scroll respeta al lector**: mientras lees hacia arriba, la vista no se mueve sola;
  solo baja si ya estabas al final.
- **Detección de turnos perdidos**: si el backend se reinicia con un turno en curso,
  al arrancar re-dispara el último mensaje sin respuesta.

## Arquitectura

```
móvil (PWA instalada)
   │ HTTPS
   ▼
Caddy ──reverse_proxy──▶ 127.0.0.1:8792
                            server.py (Python stdlib)
                              ├─ SQLite (mensajes, sesiones, adjuntos, suscripciones)
                              ├─ API local del agente  (dispara turnos y lee respuestas)
                              ├─ SMTP directo (código de acceso)
                              └─ Web Push VAPID
```

- **`server.py`** — backend completo, un solo fichero.
- **`static/`** — `index.html`, `app.js`, `theme.css`, `sw.js`, `manifest.webmanifest`, iconos.
- **`deploy/`** — unit de systemd y bloque de Caddy de ejemplo.

## Instalación rápida

1. **Copia el proyecto** al host donde corre tu agente.

```bash
git clone https://github.com/<usuario>/Agent-Chat.git
cd Agent-Chat
cp .env.example .env      # y rellena tus datos
```

2. **Genera las claves VAPID** (una vez; guárdalas):

```bash
python3 - <<'PY'
import json, base64, os
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
k = ec.generate_private_key(ec.SECP256R1())
priv = k.private_numbers().private_value.to_bytes(32, "big")
pub = k.public_key().public_bytes(serialization.Encoding.X962,
                                  serialization.PublicFormat.UncompressedPoint)
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
d = {"private_key": b64(priv), "public_key": b64(pub)}
os.makedirs("data", exist_ok=True)
open("data/vapid.json", "w").write(json.dumps(d, indent=2))
os.chmod("data/vapid.json", 0o600)
print("PUBLIC:", d["public_key"])
PY
```

3. **Instala la dependencia de push** (el resto es stdlib):

```bash
python3 -m pip install --user pywebpush
# si tu Python es gestionado por el sistema (PEP 668):
# python3 -m pip install --user --break-system-packages pywebpush
```

4. **Arranca como servicio**: adapta `deploy/chat-backend.service`
   (usuario, rutas, `EnvironmentFile`) y:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now chat-backend
```

5. **Publica por HTTPS**: añade `deploy/Caddyfile.example` a tu `Caddyfile`
   cambiando el dominio, y `sudo systemctl reload caddy`.

6. **Instálalo en el móvil**: abre el dominio en **Safari**, *Compartir → Añadir a
   pantalla de inicio*, ábrelo desde el icono y pulsa el banner **Activar** para
   conceder el permiso de notificaciones.

## Configuración

Todo va por variables de entorno, en un fichero **`.env`** (copia `.env.example`).
**Nada de esto va en el código**: así puedes publicarlo o compartirlo sin filtrar datos.

### Quién puede entrar (`CHAT_OWNER`)

Esta variable define **el único email autorizado**: a ese correo se le manda el
código de acceso de un solo uso. El email **no está en el código fuente**; lo pones tú
en tu `.env` privado:

```env
CHAT_OWNER=tu@ejemplo.com
```

Reglas:
- **Uno solo** por instancia (es un chat privado de una persona con su agente).
- En **minúsculas**; la comparación es exacta.
- Si `CHAT_OWNER` está vacío, **nadie** puede pedir token (la instancia queda cerrada
  hasta que lo configures). Es a propósito, para no dejar un chat abierto por descuido.

### Remitente del código de acceso (`CHAT_MAIL_*`, `SMTP_*`)

El código se envía por **SMTP directo al MX del dominio emisor** (sin relay).

```env
CHAT_MAIL_NAME=Agent Chat
CHAT_MAIL_FROM=no-reply@ejemplo.com
CHAT_MAIL_DOMAIN=ejemplo.com
SMTP_HOST=mx.de-ejemplo.com
SMTP_PORT=25
```

- `CHAT_MAIL_FROM` debe pertenecer a `CHAT_MAIL_DOMAIN` (es lo que se declara en el
  `EHLO`/`MAIL FROM`); si no, muchos MX lo rechazan.
- Si prefieres un relay local (Postfix en `127.0.0.1:25`), apunta `SMTP_HOST` ahí.

El resto de variables:

| Variable | Para qué |
|---|---|
| `CHAT_PORT` | puerto local (por defecto 8792) |
| `CHAT_DATA` / `CHAT_STATIC` | rutas de datos y de estáticos |
| `CHAT_BRAND` | marca que se muestra en la cabecera y en los textos |
| `CHAT_PWA_NAME` | nombre de la PWA instalada (por defecto, el del manifiesto) |
| `OMB_URL` | API local del agente (por defecto `http://127.0.0.1:8799`) |
| `OMB_BOT_ID` / `OMB_THREAD_ID` | bot e hilo donde vive la conversación |
| `CHAT_OWNER` | único email autorizado a entrar |
| `CHAT_MAIL_*` | remitente del código de acceso |
| `SMTP_HOST` / `SMTP_PORT` | servidor SMTP de salida |
| `VAPID_SUB` | contacto para las notificaciones push (`mailto:...`) |
| `CHAT_DEBUG_KEEP` | cuántos mensajes conserva el botón "Depurar" |

> **Nunca subas `.env` ni `data/` al repo.** El `.gitignore` ya los excluye.

## Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/api/request-token` | envía el código de acceso al dueño |
| `POST` | `/api/verify` | valida el código y crea la sesión |
| `POST` | `/api/logout` | cierra la sesión |
| `GET` | `/api/me` | ¿hay sesión válida? (devuelve marca y email del dueño) |
| `GET` | `/api/history` | historial de mensajes |
| `POST` | `/api/send` | envía un mensaje y dispara el turno del agente |
| `POST` | `/api/upload` | sube un adjunto |
| `GET` | `/api/file/{id}` | descarga un adjunto (requiere sesión) |
| `GET` | `/api/push/key` | clave pública VAPID |
| `POST` | `/api/push/subscribe` | registra el dispositivo |
| `POST` | `/api/push/prefs` | preferencia persistente de avisos (ON/OFF) |
| `POST` | `/api/push/status` | ¿el servidor tiene esta suscripción? |
| `POST` | `/api/push/test` | notificación de prueba |

## Seguridad

- El puerto solo escucha en **127.0.0.1**; el TLS lo hace el proxy.
- Sesión en cookie `HttpOnly`/`Secure`; token de acceso de un solo uso y caducidad corta.
- Lista blanca de estáticos servidos (nada de directorios abiertos).
- Rate limiting básico por IP en login.
- **No es cifrado extremo a extremo**: los mensajes viven en tu servidor y el push
  pasa por el servicio del fabricante (p. ej. Apple). Para algo más fuerte, usa un
  transporte cifrado propio.

## Limitaciones conocidas

- **iOS**: el push requiere iOS ≥ 16.4 y la PWA **instalada**; y **la vibración no se
  puede forzar desde la web** (la decide el sistema).
- Un solo usuario por instancia.
- Para varios usuarios/instancias, replica con otro `CHAT_PORT`, otro subdominio y
  otras claves VAPID.

## Tablas markdown

El render acepta tablas de GitHub:

```
| Columna A | Columna B |
| --- | ---: |
| valor | 42 |
```

La fila de guiones es obligatoria (dos o más guiones por celda) y admite alineación
(`:---` izquierda, `:---:` centro, `---:` derecha). Si el ancho no cabe, la tabla se
desplaza en horizontal dentro de su burbuja, sin romper el layout del chat.
Al cambiar los estáticos, sube el número de `CACHE` en `static/sw.js` para que la PWA
instalada vuelva a pedirlos.

## Contribuir

¡Bienvenido! Este proyecto nació como una herramienta personal y se publica para que
otros lo usen y lo mejoren. Ideas bienvenidas: soporte multi-usuario, borrar mensajes,
marcar leído, cifrado de extremo a extremo, más proveedores de agentes.

Abre un issue o manda un pull request. Mantén el espíritu: **un fichero de backend,
sin frameworks, fácil de auditar**.

## Licencia

MIT — ver [LICENSE](LICENSE).
