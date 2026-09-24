"""Compatibilidad vehículo↔pieza — base local (preparación Fase B).

Almacena el mapeo marca/modelo/año/motor → articulo_id de Demo Auto Parts, llenado SOLO
por confirmación de vendedores (estado='confirmada'). Vive en un archivo SQLite
APARTE de catalogo.db, porque el sync reconstruye catalogo.db con os.replace y
borraría cualquier escritura ahí. El precio/existencia se resuelven SIEMPRE desde
catalogo.db en el momento (join en Python, sin ATTACH); aquí solo se guarda el
articulo_id estable.

Plan: ~/.hermes/plans/2026-09-22_autoquote-compatibilidad-manual.md (v2, auditado).
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import yaml

try:  # normalizar() ya existe en catalogo.py; reusar para "Tsuru"/"TSURU"/"tsuru"
    from .catalogo import normalizar
except ImportError:  # pragma: no cover - cuando se corre como módulo suelto
    from catalogo import normalizar

MAX_RESULTADOS = 50  # tope anti-DoS, consistente con catalogo.py (MAX_LIMITE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS compatibilidad (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  marca_norm  TEXT NOT NULL,
  modelo_norm TEXT NOT NULL,
  anio_desde  INTEGER,
  anio_hasta  INTEGER,
  motor       TEXT,
  articulo_id INTEGER NOT NULL,
  clave       TEXT,
  nombre      TEXT,
  estado      TEXT NOT NULL DEFAULT 'confirmada',
  confirmado_por TEXT NOT NULL,
  confirmado_en  REAL NOT NULL
);
-- COALESCE en el índice único: SQLite trata NULL como DISTINTO, así que sin esto filas con
-- anio/motor NULL (="cualquier") se duplicarían. -1 y '' son centinelas seguros (años>0, motor no vacío).
CREATE UNIQUE INDEX IF NOT EXISTS ux_comp_unica
  ON compatibilidad(marca_norm, modelo_norm, COALESCE(anio_desde, -1), COALESCE(anio_hasta, -1), COALESCE(motor, ''), articulo_id);
CREATE INDEX IF NOT EXISTS idx_comp_vehiculo
  ON compatibilidad(marca_norm, modelo_norm);
"""


def _load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _db_path(config: dict) -> str:
    """Ruta de compatibilidad.db (absoluta en config; si no, relativa a la raíz del proyecto)."""
    p = config.get("compatibilidad", {}).get("path", "data/compatibilidad.db")
    if os.path.isabs(p):
        return p
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, p)


class Compatibilidad:
    """Capa de datos de compatibilidad vehículo↔pieza (lectura-escritura, WAL)."""

    def __init__(self, config_path: str = "config.yaml"):
        self._config = _load_config(config_path)
        self._path = _db_path(self._config)
        self._ensure_schema()

    # ------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        # Escritura concurrente: WAL + timeout (a diferencia de catalogo.db que es
        # immutable=1, este archivo SÍ se escribe y no se reconstruye con os.replace).
        con = sqlite3.connect(self._path, timeout=10.0)
        con.execute("PRAGMA journal_mode=WAL")
        con.row_factory = sqlite3.Row
        return con

    def _ensure_schema(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        con = self._conn()
        try:
            con.executescript(_SCHEMA)
            con.commit()
        finally:
            con.close()

    # ------------------------------------------------------------------
    def buscar(self, marca: str, modelo: str, anio: int | None = None,
               motor: str | None = None) -> list[dict]:
        """Devuelve las filas 'confirmada' que aplican al vehículo (por rango de año)."""
        marca = normalizar(marca)
        modelo = normalizar(modelo)
        motor_n = normalizar(motor) if motor else None
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT * FROM compatibilidad WHERE marca_norm = ? AND modelo_norm = ? "
                "AND estado = 'confirmada' "
                "AND (? IS NULL OR anio_desde IS NULL OR anio_desde <= ?) "
                "AND (? IS NULL OR anio_hasta IS NULL OR anio_hasta >= ?) "
                "AND (? IS NULL OR motor IS NULL OR motor = ?) "
                "ORDER BY id LIMIT ?",
                (marca, modelo, anio, anio, anio, anio, motor_n, motor_n, MAX_RESULTADOS),
            ).fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]

    def confirmar(self, marca: str, modelo: str, anio_desde: int | None,
                  anio_hasta: int | None, motor: str | None,
                  articulo_id: int, clave: str, nombre: str,
                  confirmado_por: str) -> bool:
        """Upsert de una confirmación (estado='confirmada'). Idempotente por clave única."""
        marca = normalizar(marca)
        modelo = normalizar(modelo)
        motor_n = normalizar(motor) if motor else None
        con = self._conn()
        try:
            con.execute(
                "INSERT INTO compatibilidad "
                "(marca_norm, modelo_norm, anio_desde, anio_hasta, motor, articulo_id, "
                " clave, nombre, estado, confirmado_por, confirmado_en) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmada', ?, ?) "
                "ON CONFLICT DO UPDATE SET estado='confirmada', confirmado_por=excluded.confirmado_por, "
                "confirmado_en=excluded.confirmado_en, clave=excluded.clave, nombre=excluded.nombre",
                (marca, modelo, anio_desde, anio_hasta, motor_n, articulo_id, clave, nombre,
                 confirmado_por, time.time()),
            )
            con.commit()
            return True
        except Exception:
            con.rollback()
            return False
        finally:
            con.close()
