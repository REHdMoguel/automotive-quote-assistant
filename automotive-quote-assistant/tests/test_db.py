"""Tests de la capa de datos (Fase A — SQLite local).

El bot lee de SQLite (construido por sync_catalog.py), NO de Firebird directo.
Estos tests verifican que el SQLite local devuelve datos correctos y rápidos.

Requieren:
- Haber corrido `python3 backend/sync_catalog.py` para generar data/catalogo.db
- FIREBIRD_USER / FIREBIRD_PASSWORD en el entorno (para poder re-sincronizar si hace falta).
"""

import os

import pytest

from backend.catalogo import Catalogo, normalizar


@pytest.fixture(scope="module")
def cat():
    if not _sqlite_existe():
        pytest.skip("Public edition does not include the private catalog database")
    return Catalogo(None, "config.yaml")


def _sqlite_existe() -> bool:
    import yaml

    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    path = cfg["sqlite"]["path"]
    if not os.path.isabs(path):
        base = os.path.dirname(os.path.dirname(os.path.abspath("config.yaml")))
        path = os.path.join(base, path)
    return os.path.exists(path)


def test_sqlite_existe():
    if not _sqlite_existe():
        pytest.skip("data/catalogo.db no existe; corre sync_catalog.py primero")


def test_normalizar_acentos():
    assert normalizar("BUJÍA") == "BUJIA"
    assert normalizar("aceite 15w40") == "ACEITE 15W40"


def test_buscar_por_clave(cat):
    r = cat.buscar_por_clave("77020-0K391 TOYOTA")
    assert r, "debe encontrar el artículo por clave"
    assert r[0]["nombre"] and r[0]["precio_lista"] is not None


def test_buscar_por_texto_multitoken(cat):
    r = cat.buscar_por_texto("aceite 15w40", limite=5)
    assert r, "debe encontrar aceite 15w40"
    for a in r:
        n = normalizar(a["nombre"])
        assert "ACEITE" in n and "15W40" in n


def test_precio_e_impuestos(cat):
    precio, impuestos = cat.precio_e_impuestos(803438)
    assert precio is not None
    assert any(i["impuesto_id"] == 588 for i in impuestos)


def test_buscar_cliente(cat):
    r = cat.buscar_cliente("ACOSTA", limite=5)
    assert r, "debe encontrar clientes con ACOSTA"
    assert all("ACOSTA" in normalizar(c["nombre"]) for c in r)
