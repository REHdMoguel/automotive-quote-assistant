"""Sync nocturno Firebird (Microsip) → SQLite local.

El bot NUNCA lee Firebird directamente; lee la base SQLite que este script
construye. Este script es la ÚNICA lectura diaria a Firebird (fuera de horario).

Estrategia atómica (auditada por Claude, v2):
1. Leer todo de Firebird y escribir a un archivo NUEVO `sync_YYYYMMDDHHMMSS.db`.
2. Al cerrar, `PRAGMA journal_mode=DELETE` (elimina los sidecars -wal/-shm) para
   que `os.replace` sea realmente atómico sin riesgo de mezclar páginas WAL.
3. Sanity checks: mínimo de artículos, mínimo de artículos-con-precio, mínimo de
   clientes, y comparación contra el .db anterior con tolerancia en las 3 métricas.
4. Si pasa → `os.replace(nuevo.db, produccion.db)` (atómico a nivel filesystem).
5. Si falla → dejar el .db viejo intacto, limpiar sidecars del aborto, loguear.

Reglas de datos (verificadas contra la base real):
- Precio = PRECIOS_ARTICULOS.PRECIO, PRECIO_EMPRESA_ID=42, MONEDA_ID=1 (sin IVA).
- IVA = IMPUESTOS_ARTICULOS → IMPUESTOS (588=16%, 591=0%, 592=exento).
- Existencia = CAPAS_COSTOS, CAPA_AGOTADA='N', solo almacenes vendibles.
- Charset CP1252 → UTF-8.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from decimal import Decimal
from pathlib import Path

# permitir importar backend.db cuando se ejecuta como script suelto
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db import FirebirdDB  # noqa: E402
from backend.catalogo import normalizar  # noqa: E402

# Tolerancia: si el sync baja más de este % respecto al .db anterior, abortar
MAX_DROP_TOLERANCE = 0.10


def _cargar_config(config_path: str) -> dict:
    import yaml

    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolver_sqlite_path(config: dict) -> Path:
    """Resuelve sqlite.path relativo a la ubicación del config.yaml (no al cwd).

    Igual que catalogo.py, para que sync y bot apunten SIEMPRE al mismo archivo
    aunque el sync se dispare por cron con otro cwd.
    """
    p = Path(config["sqlite"]["path"])
    if p.is_absolute():
        return p
    # relativo a la raíz del proyecto (donde vive config.yaml)
    return Path(config.get("_config_dir", ".")) / p


def _extraer_articulos(db: FirebirdDB, activo: str) -> list[tuple]:
    """Artículos activos + clave principal (rol 17) + nombre + unidad."""
    sql = (
        "SET HEADING OFF;\n"
        "SELECT COALESCE(CAST(A.ARTICULO_ID AS VARCHAR(12)),'')||'|~|'||"
        "COALESCE(TRIM(C.CLAVE_ARTICULO),'')||'|~|'||"
        "COALESCE(TRIM(A.NOMBRE),'')||'|~|'||"
        "TRIM(COALESCE(A.UNIDAD_VENTA,'')) "
        "FROM ARTICULOS A "
        "LEFT JOIN CLAVES_ARTICULOS C ON C.ARTICULO_ID=A.ARTICULO_ID "
        "AND C.ROL_CLAVE_ART_ID=17 "
        f"WHERE A.ESTATUS='{activo}';\n"
    )
    out = db.query(sql)
    filas = []
    nombres_vacios = 0
    for line in out.splitlines():
        line = line.strip()
        if not line or "|~|" not in line:
            continue
        parts = [x.strip() for x in line.split("|~|")]
        if len(parts) < 4:
            continue
        try:
            art_id = int(parts[0])
        except ValueError:
            continue
        if not parts[2]:
            nombres_vacios += 1
        filas.append((art_id, parts[1], parts[2], parts[3]))
    if nombres_vacios:
        print(f"  ⚠ {nombres_vacios} artículos con nombre vacío")
    return filas


def _extraer_precios(db: FirebirdDB, pe_id: int, moneda_id: int) -> dict[int, Decimal]:
    """Precio de lista (sin IVA) por artículo. Solo pe=42, moneda=1."""
    sql = (
        "SET HEADING OFF;\n"
        "SELECT COALESCE(CAST(ARTICULO_ID AS VARCHAR(12)),'')||'|~|'||"
        "COALESCE(CAST(PRECIO AS VARCHAR(40)),'0') "
        "FROM PRECIOS_ARTICULOS "
        f"WHERE PRECIO_EMPRESA_ID={int(pe_id)} AND MONEDA_ID={int(moneda_id)};\n"
    )
    out = db.query(sql)
    precios = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or "|~|" not in line:
            continue
        parts = [x.strip() for x in line.split("|~|")]
        if len(parts) < 2:
            continue
        try:
            art_id = int(parts[0])
            precio = Decimal(parts[1])
        except Exception:
            continue
        if precio != 0:  # 0 = sin precio configurado
            precios[art_id] = precio
    return precios


def _extraer_impuestos(db: FirebirdDB) -> dict[int, list[tuple]]:
    """Impuestos por artículo: (impuesto_id, nombre, pctje)."""
    sql = (
        "SET HEADING OFF;\n"
        "SELECT COALESCE(CAST(IA.ARTICULO_ID AS VARCHAR(12)),'')||'|~|'||"
        "COALESCE(CAST(I.IMPUESTO_ID AS VARCHAR(12)),'')||'|~|'||"
        "TRIM(I.NOMBRE)||'|~|'||"
        "COALESCE(CAST(I.PCTJE_IMPUESTO AS VARCHAR(20)),'0') "
        "FROM IMPUESTOS_ARTICULOS IA "
        "JOIN IMPUESTOS I ON I.IMPUESTO_ID=IA.IMPUESTO_ID;\n"
    )
    out = db.query(sql)
    impuestos: dict[int, list[tuple]] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or "|~|" not in line:
            continue
        parts = [x.strip() for x in line.split("|~|")]
        if len(parts) < 4:
            continue
        try:
            art_id = int(parts[0])
            imp_id = int(parts[1])
            pctje = Decimal(parts[3])
        except Exception:
            continue
        impuestos.setdefault(art_id, []).append((imp_id, parts[2], pctje))
    return impuestos


def _extraer_existencias(db: FirebirdDB, ids_vendibles: str) -> list[tuple]:
    """Existencias (suma) por artículo+almacén vendible, capa no agotada."""
    sql = (
        "SET HEADING OFF;\n"
        "SELECT COALESCE(CAST(ARTICULO_ID AS VARCHAR(12)),'')||'|~|'||"
        "COALESCE(CAST(ALMACEN_ID AS VARCHAR(12)),'')||'|~|'||"
        "COALESCE(CAST(SUM(EXISTENCIA) AS VARCHAR(30)),'0') "
        "FROM CAPAS_COSTOS "
        f"WHERE CAPA_AGOTADA='N' AND ALMACEN_ID IN ({ids_vendibles}) "
        "GROUP BY ARTICULO_ID, ALMACEN_ID;\n"
    )
    out = db.query(sql)
    filas = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "|~|" not in line:
            continue
        parts = [x.strip() for x in line.split("|~|")]
        if len(parts) < 3:
            continue
        try:
            art_id = int(parts[0])
            alm_id = int(parts[1])
            existencia = Decimal(parts[2])
        except Exception:
            continue
        filas.append((art_id, alm_id, existencia))
    return filas


def _extraer_clientes(db: FirebirdDB, activo: str) -> list[tuple]:
    """Clientes activos + RFC (best-effort; marca ambigüedad si el nombre es duplicado).

    Se extraen CLIENTES y RFCS_LCO en dos queries separadas y se cruzan en Python
    (evita la subquery correlacionada por cliente, que era O(n·m) y muy lenta).
    """
    sql_clientes = (
        "SET HEADING OFF;\n"
        "SELECT COALESCE(CAST(CL.CLIENTE_ID AS VARCHAR(12)),'')||'|~|'||"
        "TRIM(CL.NOMBRE) "
        "FROM CLIENTES CL "
        f"WHERE CL.ESTATUS='{activo}';\n"
    )
    out = db.query(sql_clientes)
    clientes = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "|~|" not in line:
            continue
        parts = [x.strip() for x in line.split("|~|")]
        if len(parts) < 2:
            continue
        try:
            cli_id = int(parts[0])
        except ValueError:
            continue
        clientes.append((cli_id, parts[1]))

    # RFCs por nombre fiscal (normalizado uppercase) → conteo para detectar ambigüedad
    sql_rfc = (
        "SET HEADING OFF;\n"
        "SELECT UPPER(TRIM(R.NOMBRE_FISCAL))||'|~|'||TRIM(R.RFC) "
        "FROM RFCS_LCO R;\n"
    )
    out = db.query(sql_rfc)
    rfc_por_nombre: dict[str, list[str]] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or "|~|" not in line:
            continue
        parts = [x.strip() for x in line.split("|~|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        rfc_por_nombre.setdefault(parts[0], []).append(parts[1])

    resultado = []
    ambiguos = 0
    for cli_id, nombre in clientes:
        rfc = ""
        rfc_list = rfc_por_nombre.get(nombre.upper(), [])
        if len(rfc_list) == 1:
            rfc = rfc_list[0]
        elif len(rfc_list) > 1:
            ambiguos += 1
        resultado.append((cli_id, nombre, rfc))
    if ambiguos:
        print(f"  ⚠ {ambiguos} clientes con nombre fiscal ambiguo (RFC omitido)")
    return resultado


def construir_db(
    db: FirebirdDB,
    destino: Path,
    config: dict,
) -> dict:
    """Construye un .db SQLite completo desde Firebird. Devuelve métricas."""
    activo = config["catalogo"]["articulo_estatus_activo"]
    pe_id = config["precio"]["precio_empresa_id"]
    moneda_id = config["precio"]["moneda_id"]
    vendibles = config["almacenes"]["vendibles"]
    ids_vendibles = ",".join(str(a["id"]) for a in vendibles)

    t0 = time.time()
    print("→ extrayendo artículos...")
    articulos = _extraer_articulos(db, activo)
    print(f"  {len(articulos)} artículos")
    print("→ extrayendo precios...")
    precios = _extraer_precios(db, pe_id, moneda_id)
    print(f"  {len(precios)} precios")
    print("→ extrayendo impuestos...")
    impuestos = _extraer_impuestos(db)
    print(f"  {len(impuestos)} con impuesto")
    print("→ extrayendo existencias...")
    existencias = _extraer_existencias(db, ids_vendibles)
    print(f"  {len(existencias)} filas de existencia")
    print("→ extrayendo clientes...")
    clientes = _extraer_clientes(db, activo)
    print(f"  {len(clientes)} clientes")

    # Escribir a SQLite nuevo
    con = sqlite3.connect(str(destino))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    cur = con.cursor()
    cur.execute("CREATE TABLE articulos (articulo_id INTEGER PRIMARY KEY, clave TEXT, nombre TEXT, nombre_norm TEXT, unidad_venta TEXT, precio_lista TEXT)")
    cur.execute("CREATE TABLE existencias (articulo_id INTEGER, almacen_id INTEGER, existencia TEXT)")
    cur.execute("CREATE TABLE clientes (cliente_id INTEGER PRIMARY KEY, nombre TEXT, nombre_norm TEXT, rfc TEXT)")
    cur.execute("CREATE TABLE impuestos (articulo_id INTEGER, impuesto_id INTEGER, nombre TEXT, pctje TEXT)")
    cur.execute("CREATE TABLE meta (clave TEXT PRIMARY KEY, valor TEXT)")
    cur.execute("CREATE INDEX idx_articulos_nombre_norm ON articulos(nombre_norm)")
    cur.execute("CREATE INDEX idx_existencias_articulo ON existencias(articulo_id)")
    cur.execute("CREATE INDEX idx_clientes_nombre_norm ON clientes(nombre_norm)")

    cur.executemany(
        "INSERT INTO articulos (articulo_id, clave, nombre, nombre_norm, unidad_venta, precio_lista) VALUES (?,?,?,?,?,?)",
        [
            (
                art_id,
                clave,
                nombre,
                normalizar(nombre),
                unidad,
                str(precios.get(art_id, "") or ""),
            )
            for (art_id, clave, nombre, unidad) in articulos
        ],
    )
    cur.executemany(
        "INSERT INTO existencias (articulo_id, almacen_id, existencia) VALUES (?,?,?)",
        [(a, m, str(e)) for (a, m, e) in existencias],
    )
    cur.executemany(
        "INSERT INTO clientes (cliente_id, nombre, nombre_norm, rfc) VALUES (?,?,?,?)",
        [(cid, nombre, normalizar(nombre), rfc) for (cid, nombre, rfc) in clientes],
    )
    filas_imp = []
    for art_id, imps in impuestos.items():
        for (imp_id, nombre, pctje) in imps:
            filas_imp.append((art_id, imp_id, nombre, str(pctje)))
    cur.executemany(
        "INSERT INTO impuestos (articulo_id, impuesto_id, nombre, pctje) VALUES (?,?,?,?)",
        filas_imp,
    )

    cur.execute(
        "INSERT INTO meta (clave, valor) VALUES ('synced_at', ?)",
        (time.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    cur.execute(
        "INSERT INTO meta (clave, valor) VALUES ('n_articulos', ?)",
        (str(len(articulos)),),
    )
    con.commit()
    # Eliminar sidecars WAL antes de publicar (os.replace atómico real)
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()

    metricas = {
        "articulos": len(articulos),
        "precios": len(precios),
        "existencias": len(existencias),
        "clientes": len(clientes),
        "tiempo_seg": round(time.time() - t0, 2),
    }
    return metricas


def _sanity_ok(nuevo_db: Path, anterior_db: Path, min_articulos: int) -> tuple[bool, str]:
    """Verifica que el .db nuevo es sano antes de publicarlo.

    Cubre: artículos, artículos-con-precio, artículos-con-impuesto, clientes y
    existencias; compara caída >10% contra el .db anterior en las 4 métricas
    principales. Los impuestos importan porque sin ellos precio_publico() trata
    al artículo como tasa 0% (cobraría el IVA de menos).
    """
    if not nuevo_db.exists():
        return False, "el .db nuevo no existe"
    con = sqlite3.connect(str(nuevo_db))
    cur = con.cursor()
    n_art = cur.execute("SELECT COUNT(*) FROM articulos").fetchone()[0]
    n_precio = cur.execute("SELECT COUNT(*) FROM articulos WHERE precio_lista != ''").fetchone()[0]
    n_imp = cur.execute("SELECT COUNT(DISTINCT articulo_id) FROM impuestos").fetchone()[0]
    n_cli = cur.execute("SELECT COUNT(*) FROM clientes").fetchone()[0]
    n_ex = cur.execute("SELECT COUNT(*) FROM existencias").fetchone()[0]
    con.close()

    if n_art < min_articulos:
        return False, f"pocos artículos ({n_art} < {min_articulos})"
    if n_precio < min_articulos * 0.5:
        return False, f"pocos artículos con precio ({n_precio})"
    if n_imp < min_articulos * 0.5:
        return False, f"pocos artículos con impuesto ({n_imp})"
    if n_cli == 0:
        return False, "cero clientes"
    if n_ex == 0:
        return False, "cero filas de existencia"

    # Comparar contra el .db anterior (tolerancia de caída)
    if anterior_db.exists():
        try:
            con = sqlite3.connect(str(anterior_db))
            cur = con.cursor()
            prev = {
                "articulos": cur.execute("SELECT COUNT(*) FROM articulos").fetchone()[0],
                "impuestos": cur.execute("SELECT COUNT(DISTINCT articulo_id) FROM impuestos").fetchone()[0],
                "clientes": cur.execute("SELECT COUNT(*) FROM clientes").fetchone()[0],
                "existencias": cur.execute("SELECT COUNT(*) FROM existencias").fetchone()[0],
            }
            con.close()
        except sqlite3.DatabaseError:
            prev = {"articulos": 0, "impuestos": 0, "clientes": 0, "existencias": 0}
        actuales = {
            "articulos": n_art,
            "impuestos": n_imp,
            "clientes": n_cli,
            "existencias": n_ex,
        }
        for metrica, actual in actuales.items():
            p = prev.get(metrica, 0)
            if p > 0 and actual < p * (1 - MAX_DROP_TOLERANCE):
                return False, f"caída de {metrica} >10% ({p} → {actual})"

    return True, f"OK ({n_art} art, {n_precio} con precio, {n_imp} con impuesto, {n_cli} clientes, {n_ex} exist)"


def _limpiar_sidecars(db_path: Path) -> None:
    for sufijo in ("-wal", "-shm"):
        side = Path(str(db_path) + sufijo)
        if side.exists():
            side.unlink(missing_ok=True)


def main(config_path: str = "config.yaml") -> int:
    import yaml

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    # registrar dir del config para resolver rutas relativas consistentemente
    config["_config_dir"] = str(Path(config_path).resolve().parent)

    sqlite_path = _resolver_sqlite_path(config)
    min_articulos = config["sqlite"].get("min_articulos", 20000)

    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    nuevo_db = sqlite_path.parent / f"sync_{time.strftime('%Y%m%d%H%M%S')}.db"

    db = FirebirdDB(config_path)
    metricas = construir_db(db, nuevo_db, config)

    ok, motivo = _sanity_ok(nuevo_db, sqlite_path, min_articulos)
    if not ok:
        print(f"✗ sync abortado: {motivo}. Se conserva el .db anterior.")
        nuevo_db.unlink(missing_ok=True)
        _limpiar_sidecars(nuevo_db)
        return 1

    # Publicar atómicamente (sin sidecars WAL ya)
    os.replace(str(nuevo_db), str(sqlite_path))
    print(f"✓ sync publicado: {sqlite_path}")
    print(f"  métricas: {metricas}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
