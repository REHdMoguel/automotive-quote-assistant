"""Reset de sesión por inactividad — bot cotizador Demo Auto Parts.

Cierra la sesión de un cliente inactivo, previo aviso, SIN afectar a otros
usuarios (por sesión/chat_id, no global). Solo DMs de Telegram (no grupos).

Lógica (acordada con el usuario, auditada por Claude v2):
1. A los 30 min de inactividad → enviar un nudge preguntando si quiere continuar.
2. Si no responde en 10 min más (40 min total) → cerrar su sesión.
3. Si responde en cualquier momento → el reloj se reinicia (no se le molesta).
4. Al cerrar, su próximo mensaje arranca de cero (nueva cotización).

Mecanismos (verificados contra el código de Hermes):
- Nudge por API DIRECTA de Telegram (sendMessage), NO `hermes send` (que insertaría
  un mensaje assistant en la sesión y reiniciaría el reloj).
- Cierre con `SessionDB.promote_to_session_reset()` (no SQL crudo): maneja filas
  recuperables y avanza conversation_generation.
- Marcas de "avisado" en JSON con escritura atómica + flock, persistiendo
  inmediatamente tras cada nudge (no en batch al final), y guardando el
  last_activity_at observado para detectar si el usuario respondió.

Corre por cron del perfil cada 5 min.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
NUDGE_MINUTOS = 30            # inactividad antes de enviar el nudge
CIERRE_MINUTOS = 40           # inactividad total antes de cerrar la sesión
MARGEN_NUDGE_FALLIDO_MIN = 15  # si el nudge falla reiteradamente, cerrar a CIERRE + esto
MAX_INTENTOS_NUDGE = 3        # intentos fallidos de nudge antes de forzar cierre
MARCA_FILE = "data/session_nudge.json"


def _hermes_home() -> Path:
    # El script vive en $HERMES_HOME/scripts/, así que el home es el padre del padre.
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def _state_db_path() -> Path:
    return _hermes_home() / "state.db"


def _marca_path() -> Path:
    return _hermes_home() / MARCA_FILE


def _hermes_source_path() -> str:
    """Ruta al checkout de Hermes, de env var o de ubicación por defecto."""
    env = os.environ.get("HERMES_SOURCE_DIR")
    if env:
        return env
    return os.path.expanduser("~/.hermes/hermes-agent")


def _cargar_dotenv() -> None:
    """Carga <home>/.env en os.environ ANTES de leer el token.

    El cron de Hermes corre el script como subproceso SIN el .env del perfil
    (env_passthrough bloquea credenciales de proveedor como TELEGRAM_BOT_TOKEN),
    así que el script debe leerlo él mismo, igual que sync_produccion.sh
    (``set -a; . .env``). El entorno del proceso (p. ej. HERMES_SOURCE_DIR vía
    env_passthrough) tiene prioridad; el .env solo rellena lo que falta.
    """
    p = _hermes_home() / ".env"
    if not p.exists():
        return
    for linea in p.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        if linea.startswith("export "):
            linea = linea[len("export "):].lstrip()
        clave, sep, valor = linea.partition("=")
        if not sep:
            continue
        clave = clave.strip()
        if not clave:
            continue
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in ("'", '"'):
            valor = valor[1:-1]
        os.environ.setdefault(clave, valor)


# ---------------------------------------------------------------------------
# Marcas de "avisado" (JSON atómico)
# ---------------------------------------------------------------------------
def _leer_marcas() -> dict:
    p = _marca_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _escribir_marcas(marcas: dict) -> None:
    p = _marca_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(marcas), encoding="utf-8")
    os.replace(str(tmp), str(p))


# ---------------------------------------------------------------------------
# Nudge por API directa de Telegram
# ---------------------------------------------------------------------------
def _enviar_nudge(chat_id: str, token: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps(
        {
            "chat_id": chat_id,
            "text": (
                "¿Aún te interesa continuar con tu cotización? Si no respondes "
                "en unos minutos, esta conversación se cerrará."
            ),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        # No imprimir el token: la URL de Telegram lo embebe y str(e) podría incluirlo.
        msg = str(e).replace(token, "***")
        print(f"  ⚠ no se pudo enviar nudge a {chat_id}: {msg}")
        return False


# ---------------------------------------------------------------------------
# Sesiones activas (solo DMs de Telegram)
# ---------------------------------------------------------------------------
def _sesiones_activas(db_path: Path) -> list[dict]:
    import sqlite3

    uri = f"file:{urllib.parse.quote(str(db_path))}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, chat_id, last_activity_at "
            "FROM sessions "
            "WHERE ended_at IS NULL AND chat_id IS NOT NULL "
            "AND chat_type = 'dm' "
            "AND session_key LIKE 'agent:autoquote-cotizador:telegram:%'"
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "session_id": r["id"],
            "chat_id": r["chat_id"],
            "last_activity_at": r["last_activity_at"] or 0,
        }
        for r in rows
    ]


def _releer_last_activity(db, session_id: str) -> float:
    """Relee last_activity_at crudo de una fila (anti-race antes de cerrar)."""
    import sqlite3

    uri = f"file:{urllib.parse.quote(str(db.db_path))}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        row = con.execute(
            "SELECT last_activity_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        con.close()
    return (row[0] if row and row[0] is not None else 0.0)


def main() -> int:
    _cargar_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("✗ TELEGRAM_BOT_TOKEN no está definido")
        return 1

    db_path = _state_db_path()
    if not db_path.exists():
        print(f"✗ state.db no existe: {db_path}")
        return 1

    sys.path.insert(0, _hermes_source_path())
    try:
        from hermes_state import SessionDB
    except ImportError as e:
        print(f"✗ no se pudo importar hermes_state desde {_hermes_source_path()}: {e}")
        return 1

    ahora = time.time()
    marcas = _leer_marcas()

    lock_path = _marca_path().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("⚠ ya hay una ejecución en curso; salgo")
            return 0

        sesiones = _sesiones_activas(db_path)

        # garbage collection: quitar marcas de sesiones que ya no están activas
        ids_activos = {s["session_id"] for s in sesiones}
        marcas = {k: v for k, v in marcas.items() if k in ids_activos}

        # un único SessionDB para todos los cierres de esta corrida
        db = SessionDB(db_path)
        try:
            for s in sesiones:
                try:
                    _procesar(s, marcas, token, ahora, db)
                except Exception as e:
                    print(f"  ⚠ error en {s['session_id']}: {e}")
        finally:
            db.close()

        _escribir_marcas(marcas)

    return 0


def _procesar(s: dict, marcas: dict, token: str, ahora: float, db) -> None:
    sid = s["session_id"]
    chat = s["chat_id"]
    last = s["last_activity_at"]
    inactivo_min = (ahora - last) / 60.0

    marca = marcas.get(sid)
    # Bloqueante #1: si el usuario respondió tras el nudge (last_activity_at avanzó),
    # invalidar la marca y volver a tratarlo como "no avisado".
    if marca is not None and last > marca.get("last_activity_at", 0):
        marcas.pop(sid, None)
        marca = None

    if inactivo_min < NUDGE_MINUTOS:
        return  # activo reciente

    if marca is None:
        # no avisado aún
        if inactivo_min >= CIERRE_MINUTOS + MARGEN_NUDGE_FALLIDO_MIN:
            # Bloqueante #4 (borde): ya pasó mucho tiempo sin nudge (nunca se pudo
            # avisar) → cerrar igual para no dejar la sesión abierta para siempre.
            _cerrar(s, marcas, db, inactivo_min)
            return
        # enviar nudge y marcar inmediatamente
        if _enviar_nudge(chat, token):
            marcas[sid] = {"last_activity_at": last, "avisado_at": ahora, "intentos": 0}
            _escribir_marcas(marcas)  # persistir YA (Bloqueante #2)
            print(f"→ nudge enviado a chat {chat} ({inactivo_min:.0f} min)")
        else:
            # nudge falló: marcar intento para reintentar/forzar cierre luego
            marcas[sid] = {"last_activity_at": last, "avisado_at": ahora, "intentos": 1}
            _escribir_marcas(marcas)
            print(f"  ⚠ nudge falló para {chat} (intento 1)")
        return

    # ya avisado
    intentos = marca.get("intentos", 0)

    # Mayor #4: si el nudge falló reiteradamente, cerrar aunque nunca llegó
    if intentos >= MAX_INTENTOS_NUDGE and inactivo_min >= NUDGE_MINUTOS:
        _cerrar(s, marcas, db, inactivo_min)
        return

    if inactivo_min >= CIERRE_MINUTOS:
        _cerrar(s, marcas, db, inactivo_min)


def _cerrar(s: dict, marcas: dict, db, inactivo_min: float) -> None:
    sid = s["session_id"]
    chat = s["chat_id"]

    # Mayor #5: anti-race — releer last_activity_at justo antes de cerrar
    actual = _releer_last_activity(db, sid)
    if actual > s["last_activity_at"]:
        # el usuario respondió en el último instante; no cerrar
        marcas.pop(sid, None)
        print(f"  ↺ sesión {sid} reactivada, no se cierra")
        return

    try:
        ok = db.promote_to_session_reset(sid, reason="session_reset")
        if ok:
            print(f"✓ sesión {sid} (chat {chat}) cerrada por inactividad {inactivo_min:.0f} min")
            marcas.pop(sid, None)
        else:
            print(f"  ⚠ no se pudo cerrar {sid}")
    except Exception as e:
        print(f"  ⚠ error cerrando {sid}: {e}")


if __name__ == "__main__":
    sys.exit(main())
