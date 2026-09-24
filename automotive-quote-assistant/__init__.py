"""Plugin "autoquote-cotizador" — tools tipadas de solo lectura para cotizar.

Este plugin es la barrera dura de seguridad: expone SOLO funciones cerradas
(buscar / precio / existencia / cotizar), de modo que el agente del perfil
dedicado NO necesita el toolset `terminal` para cotizar. Aunque un cliente
intente prompt injection, el agente solo puede llamar a estas 4 funciones de
solo lectura — no hay shell, no hay escritura, no hay `SELECT *`.

Reglas de negocio (verificadas contra la base real de Demo Auto Parts):
- Precio = PRECIOS_ARTICULOS.PRECIO, PRECIO_EMPRESA_ID=42, MONEDA_ID=1.
- IVA 16% (impuesto 588), desglosado. También 0% (591) y exento (592).
- Cálculo Decimal + ROUND_HALF_UP. Charset CP1252→UTF-8.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from decimal import Decimal

logger = logging.getLogger(__name__)

# Ruta base del plugin (para resolver config.yaml y assets/logo.jpg)
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_PLUGIN_DIR, "config.yaml")

# Límite duro de partidas por cotización (evita scraping masivo del catálogo)
MAX_PARTIDAS = 30


# ---------------------------------------------------------------------------
# Inicialización perezosa del backend (solo si se llama una tool)
# ---------------------------------------------------------------------------
_backend = None


def _get_backend():
    """Construye el backend (catalogo SQLite) una sola vez.

    El catálogo lee de SQLite local (construido por el sync nocturno), no de
    Firebird directo. `db` era FirebirdDB antes; ahora no se necesita.
    """
    global _backend
    if _backend is None:
        from .backend.catalogo import Catalogo

        _backend = Catalogo(None, _CONFIG_PATH)
    return _backend


def _articulo_a_dict(a: dict) -> dict:
    """Convierte un artículo del backend a un dict seguro (solo campos públicos).

    `precio` es el precio al público CON IVA incluido (LFPC art. 7 Bis).
    `precio_lista` se mantiene interno; el agente debe mostrar `precio`.
    """
    precio_lista = a.get("precio_lista")
    impuestos = a.get("impuestos", [])
    precio_pub = None
    if precio_lista is not None:
        from .backend.pricing import Impuesto, precio_publico
        imps = [
            Impuesto(impuesto_id=int(i["impuesto_id"]), nombre=i["nombre"],
                     pctje=Decimal(str(i["pctje"])))
            for i in impuestos
        ]
        precio_pub = precio_publico(precio_lista, imps)
    return {
        "articulo_id": a["articulo_id"],
        "clave": a["clave"],
        "nombre": a["nombre"],
        "unidad_venta": a.get("unidad_venta", ""),
        "precio": str(precio_pub) if precio_pub is not None else None,  # CON IVA
        "precio_lista": str(precio_lista) if precio_lista is not None else None,  # interno
        "impuestos": [
            {"impuesto_id": i["impuesto_id"], "nombre": i["nombre"],
             "pctje": str(i["pctje"])}
            for i in impuestos
        ],
    }


def _cotizacion_a_totales(partidas: list) -> dict:
    """Calcula subtotal/IVA/total de la cotización con el motor Decimal."""
    from .backend.pricing import Cotizacion, Impuesto

    cot = Cotizacion()
    for p in partidas:
        impuestos = [Impuesto(**i) for i in p["impuestos"]]
        cot.agregar_partida(
            clave=p["clave"],
            nombre=p["nombre"],
            cantidad=Decimal(str(p["cantidad"])),
            precio_unitario=Decimal(str(p["precio_unitario"])),
            impuestos=impuestos,
        )
    return {
        "subtotal": str(cot.subtotal_total()),
        "iva": str(cot.iva_total()),
        "total": str(cot.total()),
    }


def _cliente_a_dict(c: dict) -> dict:
    """Convierte un cliente del backend a un dict seguro (solo campos públicos)."""
    return {
        "cliente_id": c.get("cliente_id"),
        "nombre": c.get("nombre", ""),
        "rfc": c.get("rfc", ""),
    }


# ---------------------------------------------------------------------------
# Handlers de las tools
# ---------------------------------------------------------------------------
def _handle_buscar(params, **kwargs):
    del kwargs
    termino = (params.get("termino") or "").strip()
    # El clamp real vive en catalogo._clamp_limite; aquí solo convertimos sin
    # dejar que un valor inválido reviente el handler (catalogo lo acota).
    try:
        limite = int(params.get("limite", 8))
    except (TypeError, ValueError):
        limite = 8
    if not termino:
        return json.dumps({"ok": False, "error": "término vacío"})
    try:
        cat = _get_backend()
        resultados = cat.buscar_por_texto(termino, limite=limite)
        return json.dumps(
            {"ok": True, "resultados": [_articulo_a_dict(a) for a in resultados]},
            ensure_ascii=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("buscar falló: %s", e)
        return json.dumps({"ok": False, "error": "no disponible"})


def _handle_precio(params, **kwargs):
    del kwargs
    clave = (params.get("clave") or "").strip()
    if not clave:
        return json.dumps({"ok": False, "error": "clave vacía"})
    try:
        cat = _get_backend()
        resultados = cat.buscar_por_clave(clave)
        if not resultados:
            return json.dumps({"ok": False, "error": "no encontrado"})
        return json.dumps(
            {"ok": True, "articulo": _articulo_a_dict(resultados[0])},
            ensure_ascii=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("precio falló: %s", e)
        return json.dumps({"ok": False, "error": "no disponible"})


def _handle_existencia(params, **kwargs):
    del kwargs
    try:
        articulo_id = int(params.get("articulo_id"))
    except (TypeError, ValueError):
        return json.dumps({"ok": False, "error": "articulo_id inválido"})
    try:
        cat = _get_backend()
        por_sucursal = cat.existencia_por_sucursal(articulo_id)
        out = {suc: str(cant) for suc, cant in por_sucursal.items()}
        return json.dumps({"ok": True, "existencia_por_sucursal": out}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        logger.error("existencia falló: %s", e)
        return json.dumps({"ok": False, "error": "no disponible"})


def _handle_cliente(params, **kwargs):
    del kwargs
    termino = (params.get("termino") or "").strip()
    if not termino:
        return json.dumps({"ok": False, "error": "término vacío"})
    try:
        cat = _get_backend()
        resultados = cat.buscar_cliente(termino)
        return json.dumps(
            {"ok": True, "clientes": [_cliente_a_dict(c) for c in resultados]},
            ensure_ascii=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("buscar cliente falló: %s", e)
        return json.dumps({"ok": False, "error": "no disponible"})


def _handle_cotizar(params, **kwargs):
    del kwargs
    partidas = params.get("partidas") or []
    if not partidas:
        return json.dumps({"ok": False, "error": "sin partidas"})
    if len(partidas) > MAX_PARTIDAS:
        return json.dumps({"ok": False, "error": f"máximo {MAX_PARTIDAS} partidas"})
    # cliente opcional: {nombre, rfc} — si viene, se imprime; si no, "Público general"
    cliente = params.get("cliente") or None
    try:
        cat = _get_backend()
        # Validar y completar cada partida contra la base (clave → precio/impuestos)
        completas = []
        for p in partidas:
            clave = (p.get("clave") or "").strip().upper()
            try:
                cantidad = Decimal(str(p.get("cantidad")))
            except Exception:  # noqa: BLE001
                return json.dumps({"ok": False, "error": f"cantidad inválida en {clave}"})
            if cantidad <= 0:
                return json.dumps({"ok": False, "error": f"cantidad ≤ 0 en {clave}"})
            res = cat.buscar_por_clave(clave)
            if not res:
                return json.dumps({"ok": False, "error": f"clave no encontrada: {clave}"})
            art = res[0]
            if art["precio_lista"] is None:
                return json.dumps({"ok": False, "error": f"sin precio: {clave}"})
            completas.append(
                {
                    "clave": art["clave"],
                    "nombre": art["nombre"],
                    "cantidad": str(cantidad),
                    "precio_unitario": str(art["precio_lista"]),
                    "impuestos": [
                        {"impuesto_id": i["impuesto_id"], "nombre": i["nombre"],
                         "pctje": str(i["pctje"])}
                        for i in art["impuestos"]
                    ],
                }
            )

        # Calcular total CON IVA incluido (LFPC art. 7 Bis)
        from .backend.pricing import Impuesto, importe_partida, precio_publico

        total = Decimal("0")
        for p in completas:
            imps = [
                Impuesto(
                    impuesto_id=int(i["impuesto_id"]),
                    nombre=i["nombre"],
                    pctje=Decimal(str(i["pctje"])),
                )
                for i in p["impuestos"]
            ]
            p["precio_publico"] = str(
                precio_publico(Decimal(p["precio_unitario"]), imps)
            )
            total += importe_partida(Decimal(p["precio_publico"]), Decimal(p["cantidad"]))
        total = total.quantize(Decimal("0.01"))

        # Generar PDF
        from .backend.pdf import generar_pdf
        import yaml

        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        datos_empresa = cfg["empresa"]
        # cliente es {nombre, rfc} o None → "Público general"
        cliente_dict = None
        if isinstance(cliente, dict):
            cliente_dict = {
                "nombre": (cliente.get("nombre") or "").strip(),
                "rfc": (cliente.get("rfc") or "").strip(),
            }
        pdf_bytes = generar_pdf(datos_empresa, completas, cliente_dict)

        # Guardar PDF con nombre único (evita colisión entre clientes concurrentes)
        out_dir = os.path.join(_PLUGIN_DIR, "output")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"cotizacion_{uuid.uuid4().hex}.pdf")
        with open(out_path, "wb") as f:
            f.write(pdf_bytes)

        return json.dumps(
            {
                "ok": True,
                "pdf_path": out_path,
                "total": str(total),  # total CON IVA incluido
                "partidas": completas,
            },
            ensure_ascii=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("cotizar falló: %s", e)
        return json.dumps({"ok": False, "error": "no disponible"})


# ---------------------------------------------------------------------------
# Esquemas (lo que ve el LLM)
# ---------------------------------------------------------------------------
def _schema(name, description, properties, required):
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


def _check_ready() -> bool:
    """Las tools solo se exponen si la caché SQLite existe y tiene datos.

    Si `data/catalogo.db` no existe (primer arranque antes del sync), las tools
    no se exponen en vez de fallar con "no disponible" en cada consulta.
    """
    try:
        import os

        cat = _get_backend()
        # el .db debe existir y tener la tabla meta con synced_at
        if not os.path.exists(cat._path):
            return False
        import sqlite3

        con = sqlite3.connect(cat._path)
        try:
            con.execute("SELECT valor FROM meta WHERE clave='synced_at'").fetchone()
        except sqlite3.DatabaseError:
            return False
        finally:
            con.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def register(ctx) -> None:
    ctx.register_tool(
        name="autoquote_buscar_articulo",
        toolset="autoquote",
        schema=_schema(
            "autoquote_buscar_articulo",
            "Busca artículos del catálogo de Demo Auto Parts por texto (nombre o descripción). "
            "Devuelve clave, nombre y PRECIO AL PÚBLICO (con IVA incluido) de cada coincidencia. "
            "El campo 'precio' ya incluye IVA; muéstralo tal cual, sin sumarle nada.",
            {
                "termino": {"type": "string", "description": "Texto a buscar (ej. 'bujia', 'aceite 15w40')"},
                "limite": {"type": "integer", "description": "Máx resultados (default 8)"},
            },
            ["termino"],
        ),
        handler=_handle_buscar,
        check_fn=_check_ready,
        emoji="🔎",
    )

    ctx.register_tool(
        name="autoquote_precio_articulo",
        toolset="autoquote",
        schema=_schema(
            "autoquote_precio_articulo",
            "Consulta el PRECIO AL PÚBLICO (con IVA incluido) de un artículo de Demo Auto Parts por su clave. "
            "El campo 'precio' ya incluye IVA; muéstralo tal cual.",
            {"clave": {"type": "string", "description": "Clave del artículo (ej. '77020-0K391 TOYOTA')"}},
            ["clave"],
        ),
        handler=_handle_precio,
        check_fn=_check_ready,
        emoji="💲",
    )

    ctx.register_tool(
        name="autoquote_existencia",
        toolset="autoquote",
        schema=_schema(
            "autoquote_existencia",
            "Consulta la existencia disponible de un artículo por sucursal de Demo Auto Parts.",
            {"articulo_id": {"type": "integer", "description": "ID del artículo (de autoquote_buscar_articulo o autoquote_precio_articulo)"}},
            ["articulo_id"],
        ),
        handler=_handle_existencia,
        check_fn=_check_ready,
        emoji="📦",
    )

    ctx.register_tool(
        name="autoquote_buscar_cliente",
        toolset="autoquote",
        schema=_schema(
            "autoquote_buscar_cliente",
            "Busca un cliente en el catálogo de Demo Auto Parts por nombre o razón social. "
            "Devuelve cliente_id, nombre y RFC. Úsalo para identificar al cliente: "
            "si no aparece, trátalo como 'cliente general'.",
            {"termino": {"type": "string", "description": "Nombre o razón social del cliente"}},
            ["termino"],
        ),
        handler=_handle_cliente,
        check_fn=_check_ready,
        emoji="👤",
    )

    ctx.register_tool(
        name="autoquote_cotizar",
        toolset="autoquote",
        schema=_schema(
            "autoquote_cotizar",
            "Genera una cotización PDF (formato Demo Auto Parts) a partir de una lista de partidas. "
            "Cada partida: clave y cantidad. Opcionalmente recibe 'cliente' (nombre/rfc) "
            "para imprimirlo; si no se da, se imprime 'Público general'. "
            "Devuelve el total CON IVA INCLUIDO y la ruta del PDF. "
            "El PDF se adjunta con MEDIA:ruta_del_pdf en la respuesta al cliente. "
            "Los precios del PDF ya van con IVA incluido; no los desgloses.",
            {
                "partidas": {
                    "type": "array",
                    "description": "Lista de partidas con clave y cantidad",
                    "items": {
                        "type": "object",
                        "properties": {
                            "clave": {"type": "string", "description": "Clave del artículo"},
                            "cantidad": {"type": "number", "description": "Cantidad (mayor a 0)"},
                        },
                        "required": ["clave", "cantidad"],
                    },
                },
                "cliente": {
                    "type": "object",
                    "description": "Cliente opcional: {nombre, rfc}. Si se omite → 'Público general'.",
                    "properties": {
                        "nombre": {"type": "string", "description": "Nombre o razón social"},
                        "rfc": {"type": "string", "description": "RFC (opcional)"},
                    },
                },
            },
            ["partidas"],
        ),
        handler=_handle_cotizar,
        check_fn=_check_ready,
        emoji="📄",
    )
