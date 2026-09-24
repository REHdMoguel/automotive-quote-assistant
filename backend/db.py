"""Capa de acceso a datos Firebird — SOLO LECTURA.

Estrategia (decidida en Fase 1):
- El driver Python (firebird-driver/fdb) enlaza libfbclient.so.2 (Firebird 3),
  que NO lee bases ODS 13 (Firebird 5). La base de Demo Auto Parts es FB5/ODS 13.1.
- Por eso usamos `isql` de Firebird 5 dentro de un contenedor (mismo patrón
  validado en microsip-reportes). En producción cambia solo el destino
  (host/puerto del servidor real) manteniendo el mismo binario FB5.

Reglas:
- Solo SELECT. Ninguna ruta INSERT/UPDATE/DELETE.
- Credenciales por variables de entorno (ISC_USER / ISC_PASSWORD), nunca en
  argumentos visibles en `ps`.
- Decodificación CP1252 → UTF-8 en todo texto (la base es RDB$CHARSET NONE,
  datos legacy en WIN1252/CP1252).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any

import yaml

# Patrones de error que isql puede emitir en stdout (no solo stderr) con rc=0
_ERROR_PATTERNS = re.compile(
    r"Statement failed|SQL error|Dynamic SQL Error|-arithmetic exception|"
    r"no permission|Table unknown|Column unknown|Invalid token|SQLSTATE",
    re.IGNORECASE,
)

# Solo se permiten statements de lectura (defensa en profundidad)
_SELECT_ONLY = re.compile(r"^\s*(SELECT|SET)\b", re.IGNORECASE)


def _get_secret(name: str, default: str | None = None) -> str | None:
    """Resuelve un secreto respetando el scope del perfil (multiplex-safe).

    Usa agent.secret_scope.get_secret cuando está disponible (gateway multiplex),
    con fallback a os.environ para single-profile/dev.
    """
    try:
        from agent.secret_scope import get_secret
        val = get_secret(name, default)
        if val is not None:
            return val
    except Exception:
        pass
    return os.environ.get(name, default)


@dataclass
class FirebirdConfig:
    modo: str
    isql_bin: str
    contenedor_imagen: str
    archivo: str | None
    mount: str | None
    host: str | None
    puerto: int
    ruta: str | None
    timeout_seg: int
    solo_lectura: bool


def _load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _build_connstr(cfg: FirebirdConfig) -> str:
    if cfg.modo == "local":
        return cfg.archivo or ""
    # red: host/puerto:ruta
    return f"{cfg.host}/{cfg.puerto}:{cfg.ruta}"


def _isql_command(cfg: FirebirdConfig, connstr: str) -> list[str]:
    """Construye el comando podman para ejecutar isql FB5 contra la base.

    Reenvía ISC_USER/ISC_PASSWORD al contenedor con --env: podman NO propaga
    el entorno del host por defecto, así que sin esto isql arranca sin
    credenciales (solo funcionaría con el default SYSDBA/masterkey del
    contenedor, que NO es el caso de la base de producción).
    """
    base = [
        "podman", "run", "--rm", "-i",
        "--env", "ISC_USER", "--env", "ISC_PASSWORD",
    ]
    if cfg.modo == "local":
        return base + [
            "-v", f"{cfg.mount}:/app/data:rw",
            cfg.contenedor_imagen,
            cfg.isql_bin,
            connstr,
            "-q",
        ]
    # red: sin mount, conexión por TCP
    return base + [
        cfg.contenedor_imagen,
        cfg.isql_bin,
        connstr,
        "-q",
    ]


class FirebirdDB:
    """Wrapper de solo lectura sobre Firebird 5 vía isql en contenedor."""

    def __init__(self, config_path: str = "config.yaml"):
        raw = _load_config(config_path)
        fb = raw["firebird"]
        # El mount (ruta del host) se lee del secret scope con fallback al config,
        # para no versionar rutas absolutas del desarrollador en el repo.
        mount = _get_secret("FIREBIRD_MOUNT") or fb.get("local", {}).get("mount")
        self.cfg = FirebirdConfig(
            modo=fb["modo"],
            isql_bin=fb["isql_bin"],
            contenedor_imagen=fb["contenedor_imagen"],
            archivo=fb.get("local", {}).get("archivo"),
            mount=mount,
            host=fb.get("red", {}).get("host"),
            puerto=fb.get("red", {}).get("puerto", 3050),
            ruta=fb.get("red", {}).get("ruta"),
            timeout_seg=fb.get("timeout_seg", 30),
            solo_lectura=fb.get("solo_lectura", True),
        )
        self._user_env = fb.get("usuario_env", "ISC_USER")
        self._pass_env = fb.get("password_env", "ISC_PASSWORD")

    # ------------------------------------------------------------------
    # utilidades
    # ------------------------------------------------------------------
    def _decode(self, data: bytes) -> str:
        """isql emite Latin-1/CP1252; decodificamos CP1252 y normalizamos."""
        return data.decode("cp1252", errors="replace")

    def _env(self) -> dict[str, str]:
        """Env mínimo para el contenedor: solo credenciales Firebird.

        NO copiar todo os.environ — evita filtrar API keys/tokens del perfil
        hacia el contenedor efímero. Las credenciales se resuelven con
        get_secret() (respeta el scope del perfil bajo multiplex), con fallback
        a os.environ para single-profile.

        Los NOMBRES en el contenedor son SIEMPRE ISC_USER/ISC_PASSWORD (los que
        isql espera), aunque la FUENTE del secreto sea otra variable (p. ej.
        FIREBIRD_USER para la base de producción). La fuente se lee de
        usuario_env/password_env; el destino es fijo.
        """
        env = {}
        user = _get_secret(self._user_env)
        password = _get_secret(self._pass_env)
        # Fail-closed: en modo red, si las credenciales no se resuelven, abortar
        # con un error de CONFIGURACIÓN claro. De lo contrario isql arranca sin
        # credenciales y falla con un error de auth genérico de Firebird que
        # cuesta depurar (vs un mensaje de "revisa FIREBIRD_USER en el .env").
        if self.cfg.modo == "red" and (not user or not password):
            raise RuntimeError(
                f"credenciales de producción no resueltas: revisa "
                f"{self._user_env}/{self._pass_env} en el .env del perfil"
            )
        if user:
            env["ISC_USER"] = user
        if password:
            env["ISC_PASSWORD"] = password
        env["PATH"] = os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        return env

    def query(self, sql: str) -> str:
        """Ejecuta un SELECT y devuelve la salida cruda decodificada (CP1252→UTF-8).

        La convención de salida es `SET HEADING OFF` + columnas concatenadas con
        `|| '\\t' ||`. El llamador parsea.
        """
        # defensa en profundidad: solo statements de lectura
        if not _SELECT_ONLY.match(sql.strip()):
            raise ValueError("Solo se permiten statements SELECT/SET (solo lectura)")

        connstr = _build_connstr(self.cfg)
        cmd = _isql_command(self.cfg, connstr)
        env = self._env()
        try:
            proc = subprocess.run(
                cmd,
                input=sql.encode("utf-8"),
                capture_output=True,
                timeout=self.cfg.timeout_seg,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError(
                f"Firebird query superó {self.cfg.timeout_seg}s"
            )
        stdout = self._decode(proc.stdout)
        stderr = self._decode(proc.stderr)
        # isql puede emitir errores en stdout con returncode 0
        if proc.returncode != 0 or _ERROR_PATTERNS.search(stdout):
            raise RuntimeError(
                f"isql error (rc={proc.returncode}): {stderr.strip() or stdout.strip()}"
            )
        return stdout

    def query_rows(self, sql: str, delimiter: str = "\t") -> list[list[str]]:
        """Ejecuta SELECT y devuelve filas parseadas por delimiter.

        Se asume que el SQL ya produce columnas concatenadas con `|| '<delim>' ||`
        y `COALESCE` en toda columna (NULL rompería la fila).
        """
        out = self.query(sql)
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(line.split(delimiter))
        return rows
