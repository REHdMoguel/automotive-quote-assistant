"""Búsqueda de artículos en el catálogo — lee de SQLite local (Fase A).

El bot NO lee Firebird directamente; lee la base SQLite que construye el sync
nocturno (`sync_microsip.py`). Reglas:

- Solo artículos presentes en SQLite (el sync ya filtra ACTIVOS).
- Búsqueda por clave exacta y por texto (normalizando acentos).
- Precio de lista (sin IVA) e IVA vienen de las tablas `articulos`/`impuestos`.
- Existencia por almacén vendible → sucursal.

La interfaz es idéntica a la versión Firebird anterior, para no romper el
plugin `__init__.py`.

Seguridad/concurrencia (auditado por Claude):
- Conexión de solo lectura e inmutable (no toca -wal/-shm durante os.replace).
- LIKE con ESCAPE para que `%`/`_` se traten como literales, no comodines.
"""

from __future__ import annotations

import os
import sqlite3
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

import yaml

# Límites defensivos (auditado por Claude): un `limite` negativo en SQLite
# equivale a "sin límite" (fugaría el catálogo completo vía prompt injection),
# y un término con decenas de palabras genera decenas de condiciones LIKE
# (DoS barato). Estos topes son duros, no sugerencias.
MAX_LIMITE = 50
MAX_TOKENS = 6


def normalizar(texto: str) -> str:
    """Quita acentos y pasa a mayúsculas para comparación.

    Nota (decisión intencional): NFD descompone "Ñ" en "N" + tilde combinante,
    que se elimina igual que un acento, por lo que normalizar("AÑO") ==
    normalizar("ANO") y normalizar("NIÑO") == normalizar("NINO"). Es tolerancia
    de búsqueda (muchos usuarios no tipean "ñ"); no cambiar sin re-evaluar.
    """
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return texto.upper()


def _clamp_limite(limite: Any) -> int:
    """Convierte `limite` a un entero seguro entre 1 y MAX_LIMITE."""
    try:
        n = int(limite)
    except (TypeError, ValueError):
        n = 10
    return max(1, min(n, MAX_LIMITE))


def _tokens(termino: str) -> list[str]:
    """Normaliza y parte en tokens, limitando la cantidad (anti-DoS)."""
    tokens = [t for t in normalizar(termino).split() if t]
    return tokens[:MAX_TOKENS]


def _load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _sqlite_path(config: dict) -> str:
    """Ruta del .db SQLite (relativa a la raíz del proyecto)."""
    path = config["sqlite"]["path"]
    if not os.path.isabs(path):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base, path)
    return path


def _escape_like(token: str) -> str:
    """Escapa %, _ y \\ para que LIKE los trate como literales (con ESCAPE '\\')."""
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Catalogo:
    """Catálogo de artículos respaldado por SQLite local (rápido, no toca Firebird)."""

    def __init__(self, db=None, config_path: str = "config.yaml"):
        # `db` se ignora (compatibilidad de firma con la versión Firebird).
        self._config = _load_config(config_path)
        self._path = _sqlite_path(self._config)
        self._sucursal_por_almacen = {
            a["id"]: a["sucursal"] for a in self._config["almacenes"]["vendibles"]
        }
        self._ids_vendibles = set(
            a["id"] for a in self._config["almacenes"]["vendibles"]
        )

    # ------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        # Solo lectura + inmutable: no crea sidecars ni toca -wal/-shm, y es
        # seguro durante el os.replace del sync (mantiene el fd al archivo
        # abierto). `immutable=1` evita que SQLite intente locks/verificaciones
        # extra, ya que el sync garantiza que cada .db es un archivo completo e
        # inmutable publicado atómicamente.
        uri = f"file:{self._path}?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        return con

    # ------------------------------------------------------------------
    def buscar_por_clave(self, clave: str) -> list[dict]:
        """Busca por clave exacta (orden determinístico por articulo_id)."""
        clave = clave.strip().upper()
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT articulo_id, clave, nombre, unidad_venta, precio_lista "
                "FROM articulos WHERE UPPER(TRIM(clave)) = ? "
                "ORDER BY articulo_id LIMIT ?",
                (clave, MAX_LIMITE),
            ).fetchall()
        finally:
            con.close()
        return self._articulos_a_dicts(rows)

    def buscar_por_texto(self, termino: str, limite: int = 10) -> list[dict]:
        """Busca por texto (tokens con AND) sobre nombre_norm, escapando LIKE."""
        tokens = _tokens(termino)
        if not tokens:
            return []
        limite = _clamp_limite(limite)
        conds = " AND ".join(["nombre_norm LIKE ? ESCAPE '\\'"] * len(tokens))
        params = [f"%{_escape_like(t)}%" for t in tokens] + [limite]
        con = self._conn()
        try:
            rows = con.execute(
                f"SELECT articulo_id, clave, nombre, unidad_venta, precio_lista "
                f"FROM articulos WHERE {conds} ORDER BY articulo_id LIMIT ?",
                params,
            ).fetchall()
        finally:
            con.close()
        return self._articulos_a_dicts(rows)

    # ------------------------------------------------------------------
    def precio_e_impuestos(self, articulo_id: int) -> tuple[Decimal | None, list[dict]]:
        con = self._conn()
        try:
            row = con.execute(
                "SELECT precio_lista FROM articulos WHERE articulo_id = ?",
                (int(articulo_id),),
            ).fetchone()
        finally:
            con.close()
        precio = None
        if row and row["precio_lista"]:
            try:
                precio = Decimal(row["precio_lista"])
            except Exception:
                precio = None
        impuestos = self._impuestos(articulo_id)
        return precio, impuestos

    def articulo_por_id(self, articulo_id: int) -> dict | None:
        """Devuelve el artículo completo (clave, nombre, precio_lista, impuestos) por id."""
        con = self._conn()
        try:
            row = con.execute(
                "SELECT articulo_id, clave, nombre, unidad_venta, precio_lista "
                "FROM articulos WHERE articulo_id = ?",
                (int(articulo_id),),
            ).fetchone()
        finally:
            con.close()
        if not row:
            return None
        return self._articulo_a_dict(row)

    def _impuestos(self, articulo_id: int) -> list[dict]:
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT impuesto_id, nombre, pctje FROM impuestos WHERE articulo_id = ?",
                (int(articulo_id),),
            ).fetchall()
        finally:
            con.close()
        return self._filas_a_impuestos(rows)

    def _impuestos_batch(self, articulo_ids: list[int]) -> dict[int, list[dict]]:
        """Impuestos de varios artículos en una sola query (evita N+1)."""
        if not articulo_ids:
            return {}
        con = self._conn()
        try:
            placeholders = ",".join("?" * len(articulo_ids))
            rows = con.execute(
                f"SELECT articulo_id, impuesto_id, nombre, pctje FROM impuestos "
                f"WHERE articulo_id IN ({placeholders})",
                articulo_ids,
            ).fetchall()
        finally:
            con.close()
        out: dict[int, list[dict]] = {}
        for r in rows:
            out.setdefault(r["articulo_id"], []).extend(self._fila_a_impuesto(r))
        return out

    @staticmethod
    def _fila_a_impuesto(r: sqlite3.Row) -> list[dict]:
        """Convierte una fila de impuestos a dict, omitiendo pctje corrupto.

        Si Decimal(pctje) falla (dato corrupto del sync), se omite ESE impuesto
        en vez de tumbar el batch completo de una búsqueda.
        """
        try:
            pctje = Decimal(r["pctje"])
        except (InvalidOperation, TypeError, ValueError):
            return []
        return [{"impuesto_id": r["impuesto_id"], "nombre": r["nombre"], "pctje": pctje}]

    def _filas_a_impuestos(self, rows: list[sqlite3.Row]) -> list[dict]:
        out = []
        for r in rows:
            out.extend(self._fila_a_impuesto(r))
        return out

    # ------------------------------------------------------------------
    def existencia_por_sucursal(self, articulo_id: int) -> dict[str, Decimal]:
        # JOIN contra articulos: solo devolver existencia si el artículo existe
        # (activo) en el catálogo; evita cotizar artículos dados de baja.
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT e.almacen_id, e.existencia FROM existencias e "
                "JOIN articulos a ON a.articulo_id = e.articulo_id "
                "WHERE e.articulo_id = ?",
                (int(articulo_id),),
            ).fetchall()
        finally:
            con.close()
        por_sucursal: dict[str, Decimal] = {}
        for r in rows:
            alm_id = r["almacen_id"]
            if alm_id not in self._ids_vendibles:
                continue
            suc = self._sucursal_por_almacen.get(alm_id, f"Almacén {alm_id}")
            try:
                por_sucursal[suc] = por_sucursal.get(suc, Decimal("0")) + Decimal(
                    r["existencia"]
                )
            except Exception:
                continue
        return por_sucursal

    # ------------------------------------------------------------------
    def buscar_cliente(self, termino: str, limite: int = 8) -> list[dict]:
        tokens = _tokens(termino)
        if not tokens:
            return []
        limite = _clamp_limite(limite)
        conds = " AND ".join(["nombre_norm LIKE ? ESCAPE '\\'"] * len(tokens))
        params = [f"%{_escape_like(t)}%" for t in tokens] + [limite]
        con = self._conn()
        try:
            rows = con.execute(
                f"SELECT cliente_id, nombre, rfc FROM clientes WHERE {conds} "
                f"ORDER BY cliente_id LIMIT ?",
                params,
            ).fetchall()
        finally:
            con.close()
        return [
            {"cliente_id": r["cliente_id"], "nombre": r["nombre"], "rfc": r["rfc"]}
            for r in rows
        ]

    # ------------------------------------------------------------------
    def _articulos_a_dicts(self, rows: list[sqlite3.Row]) -> list[dict]:
        """Convierte varias filas a dicts, resolviendo impuestos en batch (sin N+1)."""
        if not rows:
            return []
        ids = [r["articulo_id"] for r in rows]
        impuestos = self._impuestos_batch(ids)
        out = []
        for r in rows:
            out.append(self._articulo_a_dict(r, impuestos.get(r["articulo_id"], [])))
        return out

    def _articulo_a_dict(self, r: sqlite3.Row, impuestos: list[dict] | None = None) -> dict:
        precio = None
        if r["precio_lista"]:
            try:
                precio = Decimal(r["precio_lista"])
            except Exception:
                precio = None
        if impuestos is None:
            impuestos = self._impuestos(r["articulo_id"])
        return {
            "articulo_id": r["articulo_id"],
            "clave": r["clave"],
            "nombre": r["nombre"],
            "unidad_venta": r["unidad_venta"],
            "precio_lista": precio,
            "impuestos": impuestos,
        }
