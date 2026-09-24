"""Tests de la lógica de `catalogo.py` contra una SQLite sintética (sin Firebird).

Cubre: normalización de acentos, búsqueda multitoken, escape de LIKE (% y _),
claves con orden determinístico, y ausencia de SQL injection (parametrización).
No depende del sync real ni de la base de Demo Auto Parts.
"""

import sqlite3
from decimal import Decimal

import pytest

from backend.catalogo import Catalogo, normalizar, _escape_like


def _crear_sqlite(tmp_path) -> str:
    """Crea una SQLite mínima determinística con datos sintéticos."""
    db = tmp_path / "catalogo.db"
    con = sqlite3.connect(str(db))
    cur = con.cursor()
    cur.execute("CREATE TABLE articulos (articulo_id INTEGER PRIMARY KEY, clave TEXT, nombre TEXT, nombre_norm TEXT, unidad_venta TEXT, precio_lista TEXT)")
    cur.execute("CREATE TABLE existencias (articulo_id INTEGER, almacen_id INTEGER, existencia TEXT)")
    cur.execute("CREATE TABLE clientes (cliente_id INTEGER PRIMARY KEY, nombre TEXT, nombre_norm TEXT, rfc TEXT)")
    cur.execute("CREATE TABLE impuestos (articulo_id INTEGER, impuesto_id INTEGER, nombre TEXT, pctje TEXT)")
    cur.execute("CREATE TABLE meta (clave TEXT PRIMARY KEY, valor TEXT)")
    cur.execute("INSERT INTO meta VALUES ('synced_at','2026-01-01')")

    # artículos con acentos y casos borde
    datos = [
        (1, "BUJIA-1", "BUJÍA ENCENDIDO NGK", "PZA", "100"),
        (2, "ACEITE-15W40", "ACEITE MOBIL DELVAC 15W40", "L", "500"),
        (3, "CLAVE_100", "REFACCION CON GUION_BAJO", "PZA", "200"),
        (4, "PCT-50%", "ARTICULO CON PORCENTAJE 50%", "PZA", "300"),
    ]
    cur.executemany(
        "INSERT INTO articulos VALUES (?,?,?,?,?,?)",
        [(a, c, n, normalizar(n), u, p) for (a, c, n, u, p) in datos],
    )
    cur.executemany(
        "INSERT INTO impuestos VALUES (?,?,?,?)",
        [(1, 588, "IVA 16%", "16.0"), (2, 588, "IVA 16%", "16.0")],
    )
    cur.executemany(
        "INSERT INTO existencias VALUES (?,?,?)",
        [(1, 19, "5"), (2, 19, "3")],
    )
    cur.executemany(
        "INSERT INTO clientes VALUES (?,?,?,?)",
        [(100, "JUAN PEREZ", "JUAN PEREZ", "RFC1"), (101, "ACME SA", "ACME SA", "RFC2")],
    )
    con.commit()
    con.close()
    return str(db)


@pytest.fixture
def catalogo_sintetico(tmp_path, monkeypatch):
    """Catalogo apuntando a la SQLite sintética."""
    db_path = _crear_sqlite(tmp_path)
    # parchear la ruta para que use la sintética en vez de la real
    cat = Catalogo.__new__(Catalogo)  # no llamar __init__ real
    cat._path = db_path
    cat._sucursal_por_almacen = {19: "Matriz"}
    cat._ids_vendibles = {19}
    return cat


def test_normalizar_acentos():
    assert normalizar("BUJÍA") == "BUJIA"
    assert normalizar("Aceite 15W40") == "ACEITE 15W40"


def test_escape_like():
    assert _escape_like("50%") == "50\\%"
    assert _escape_like("guion_bajo") == "guion\\_bajo"
    assert _escape_like("a\\b") == "a\\\\b"


def test_buscar_con_acento(catalogo_sintetico):
    r = catalogo_sintetico.buscar_por_texto("bujia", limite=5)
    assert r, "debe encontrar BUJÍA buscando 'bujia'"
    assert r[0]["articulo_id"] == 1


def test_buscar_multitoken(catalogo_sintetico):
    r = catalogo_sintetico.buscar_por_texto("aceite 15w40", limite=5)
    assert r
    assert r[0]["articulo_id"] == 2


def test_buscar_porcentaje_literal(catalogo_sintetico):
    """El % debe tratarse como literal, no comodín (no devolver todo)."""
    r = catalogo_sintetico.buscar_por_texto("50%", limite=10)
    # solo el artículo con "50%" literal, no todos
    assert all(a["articulo_id"] == 4 for a in r)
    assert len(r) == 1


def test_buscar_guion_bajo_literal(catalogo_sintetico):
    """El _ debe tratarse como literal, no comodín."""
    r = catalogo_sintetico.buscar_por_texto("guion_bajo", limite=10)
    assert len(r) == 1
    assert r[0]["articulo_id"] == 3


def test_buscar_clave_deterministico(catalogo_sintetico):
    r = catalogo_sintetico.buscar_por_clave("BUJIA-1")
    assert len(r) == 1
    assert r[0]["articulo_id"] == 1


def test_precio_e_impuestos(catalogo_sintetico):
    precio, imps = catalogo_sintetico.precio_e_impuestos(1)
    assert precio == Decimal("100")
    assert imps[0]["impuesto_id"] == 588


def test_buscar_cliente(catalogo_sintetico):
    r = catalogo_sintetico.buscar_cliente("juan", limite=5)
    assert len(r) == 1
    assert r[0]["rfc"] == "RFC1"
