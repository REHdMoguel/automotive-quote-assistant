"""Generador de PDF de cotización — réplica del formato "COTIZACION MATRIZ" de Demo Auto Parts.

Layout extraído del template ReportBuilder real (`Cotizacion Demo Auto Parts.rtm`):
- Papel Carta (215.9 × 279.4 mm), márgenes 6.35 mm.
- Encabezado: logo + razón social + RFC + dirección + email + leyendas.
- Título "Cotización", folio/vigencia/cliente/vendedor.
- Tabla de partidas: Artículo / Nombre / Unidades / Precio / Importe.
- Bloque de totales: Total (con IVA incluido).

Cumplimiento legal (LFPC art. 7 Bis): los precios se exhiben CON IVA incluido,
sin desglose de subtotal/IVA. El desglose fiscal va en la factura (CFDI), no en
la cotización al consumidor final.

Nota: el template original tiene además "U.med." y "Descto."; en esta fase el
bot cotiza a precio de lista sin descuentos, por lo que esas dos columnas se
omiten (a implementar si el negocio requiere descuentos por cliente).

Regla monetaria: Decimal + ROUND_HALF_UP, precio público = lista × (1 + IVA).
"""

from __future__ import annotations

from decimal import Decimal

from .pricing import Cotizacion, Impuesto, _round_cents, precio_publico, importe_partida


def _money(v: Decimal) -> str:
    """Formatea dinero redondeando a centavos con ROUND_HALF_UP (consistente
    con pricing.py). Nunca formatear un Decimal crudo de 6 decimales."""
    return f"{_round_cents(v):,.2f}"


def _fmt_cantidad(v: Decimal) -> str:
    """Formatea una cantidad sin truncar fracciones (2.5 → "2.5", 2 → "2").

    Evita el bug de mostrar "2" cuando se cobró 2.5. Soporta hasta 3 decimales
    (unidades fraccionarias tipo litros); elimina ceros y punto sobrante.
    """
    s = f"{v:,.3f}".rstrip("0").rstrip(".")
    return s


def generar_pdf(datos_empresa: dict, partidas: list[dict], cliente: dict | None = None) -> bytes:
    """Devuelve los bytes del PDF.

    datos_empresa: claves razon_social, rfc, direccion, telefonos, email,
                   leyenda_1, leyenda_2.
    partidas: [{clave, nombre, cantidad, precio_unitario, impuestos}].
              `precio_unitario` es el precio SIN IVA (de la base).
    cliente: {nombre, rfc} opcional — si es None se muestra "Público general".

    Precios exhibidos CON IVA incluido (LFPC art. 7 Bis). Sin desglose de
    subtotal/IVA: el cliente ve un único precio final por partida y un total.
    """
    cot = Cotizacion()
    for p in partidas:
        impuestos = [
            Impuesto(
                impuesto_id=int(i["impuesto_id"]),
                nombre=i["nombre"],
                pctje=Decimal(str(i["pctje"])),
            )
            for i in p["impuestos"]
        ]
        cot.agregar_partida(
            clave=p["clave"],
            nombre=p["nombre"],
            cantidad=Decimal(str(p["cantidad"])),
            precio_unitario=Decimal(str(p["precio_unitario"])),
            impuestos=impuestos,
        )

    # Precios CON IVA (lo que ve el cliente)
    total = Decimal("0")
    filas = []
    for p in cot.partidas:
        precio_pub = precio_publico(p["precio_unitario"], p["impuestos"])
        importe = importe_partida(precio_pub, p["cantidad"])
        total += importe
        cantidad = _fmt_cantidad(p["cantidad"])
        filas.append(
            "<tr>"
            f"<td class='clave'>{p['clave']}</td>"
            f"<td>{p['nombre']}</td>"
            f"<td class='c'>{cantidad}</td>"
            f"<td class='r'>{_money(precio_pub)}</td>"
            f"<td class='r'>{_money(importe)}</td>"
            "</tr>"
        )
    total = _round_cents(total)

    # --- construir HTML con el layout de Demo Auto Parts ---
    telefonos = " / ".join(datos_empresa.get("telefonos", []))

    html = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<style>
@page {{ size: Letter; margin: 6.35mm; }}
body {{ font-family: 'DejaVu Sans', sans-serif; font-size: 10px; color: #000; }}
.encabezado {{ display: flex; align-items: center; justify-content: space-between; border-bottom: 2px solid #000; padding-bottom: 6px; margin-bottom: 8px; }}
.logo img {{ height: 55px; }}
.datos {{ flex: 1; margin-left: 12px; }}
.razon {{ font-size: 16px; font-weight: bold; }}
.rfc {{ font-size: 11px; }}
.dir {{ font-size: 9px; color: #333; }}
.leyendas {{ text-align: right; font-size: 9px; }}
.leyenda-1 {{ font-weight: bold; }}
.titulo {{ text-align: center; font-size: 15px; font-weight: bold; margin: 10px 0; }}
.meta {{ display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 10px; }}
.meta .campo b {{ display: inline-block; min-width: 70px; }}
table.partidas {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
table.partidas th {{ background: #e8e8e8; border: 1px solid #000; padding: 4px; font-size: 9px; text-align: left; }}
table.partidas td {{ border: 1px solid #000; padding: 4px; font-size: 9px; }}
td.clave {{ white-space: nowrap; }}
td.c {{ text-align: center; }}
td.r {{ text-align: right; }}
.totales {{ margin-top: 10px; width: 45%; margin-left: 55%; font-size: 10px; }}
.totales table {{ width: 100%; border-collapse: collapse; }}
.totales td {{ padding: 3px 6px; }}
.totales .total {{ font-weight: bold; border-top: 1px solid #000; font-size: 12px; }}
.iva-nota {{ font-size: 8px; color: #555; text-align: right; margin-top: 2px; }}
.condiciones {{ margin-top: 20px; font-size: 9px; }}
</style></head><body>

<div class="encabezado">
  <div class="logo"><strong>AUTOQUOTE</strong></div>
  <div class="datos">
    <div class="razon">{datos_empresa.get('razon_social','')}</div>
    <div class="rfc">RFC: {datos_empresa.get('rfc','')}</div>
    <div class="dir">{datos_empresa.get('direccion','')}</div>
    <div class="dir">Tel: {telefonos} · {datos_empresa.get('email','')}</div>
  </div>
  <div class="leyendas">
    <div class="leyenda-1">{datos_empresa.get('leyenda_1','')}</div>
    <div>{datos_empresa.get('leyenda_2','')}</div>
  </div>
</div>

<div class="titulo">COTIZACIÓN</div>

<div class="meta">
  <div>
    <div class="campo"><b>Cliente:</b> {_cliente_label(cliente)}</div>
    <div class="campo"><b>Vigencia:</b> 7 días</div>
  </div>
  <div>
    <div class="campo"><b>Fecha:</b> {_fecha_actual()}</div>
  </div>
</div>

<table class="partidas">
  <thead>
    <tr>
      <th>Artículo</th><th>Nombre</th><th>Unidades</th><th>Precio</th><th>Importe</th>
    </tr>
  </thead>
  <tbody>
    {''.join(filas)}
  </tbody>
</table>

<div class="totales">
  <table>
    <tr class="total"><td>Total</td><td class="r">{_money(total)}</td></tr>
  </table>
  <p class="iva-nota">Precios con IVA incluido.</p>
</div>

<div class="condiciones">
  Precios sujetos a cambio sin previo aviso. Existencia sujeta a disponibilidad.
</div>

</body></html>"""

    # renderizar con weasyprint (logo como base_url)
    from weasyprint import HTML

    import os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return HTML(string=html, base_url=base).write_pdf()


def _fecha_actual() -> str:
    from datetime import date

    return date.today().strftime("%d/%m/%Y")


def _cliente_label(cliente: dict | None) -> str:
    """Etiqueta del cliente en el PDF: nombre (+ RFC) o 'Público general'."""
    if not cliente or not cliente.get("nombre"):
        return "Público general"
    nombre = cliente["nombre"].strip()
    rfc = (cliente.get("rfc") or "").strip()
    return f"{nombre} · RFC {rfc}" if rfc else nombre
