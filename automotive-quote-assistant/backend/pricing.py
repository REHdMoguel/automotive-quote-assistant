"""Motor de cálculo de precios — regla definitiva (auditada).

Regla monetaria:
- Todo el pipeline usa `decimal.Decimal`, NUNCA `float`.
- El precio se mantiene con precisión completa (6 decimales) durante TODOS los
  cálculos (precio × cantidad, suma de subtotales, IVA).
- El IVA se calcula POR PARTIDA (precio × cantidad × tasa) y se suman los IVAs.
- El redondeo a centavos se aplica SOLO al final (cada partida y el total),
  con ROUND_HALF_UP. Nunca `round()` nativo (banker's rounding).

Ejemplo sintético de validación:
  FCS001590: 12,100 × 4 = 48,400 neto; 48,400 × 0.16 = 7,744 IVA  ✅

Nota de mantenimiento (distinción VE vs PV — verificado en la base real):
  - COTIZACIONES y ÓRDENES DE VENTA (DOCTOS_VE tipo C/P) redondean el total a
    CENTAVOS con ROUND_HALF_UP. Este bot emite cotizaciones → esta es la regla.
  - PUNTO DE VENTA (DOCTOS_PV) fija un precio CON IVA a peso entero (factor de
    redondeo de la tabla de precios de mostrador), NO es una regla de redondeo
    de totales de documento. No replicar aquí: el bot cotiza como VE.
  No "corrijas" esto hacia redondeo a pesos enteros por ver "Factor 0.99" en
  Preferencias de Microsip — es una capa distinta (fijación de precio de
  mostrador), no el redondeo aritmético de una cotización.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

CENTAVOS = Decimal("0.01")


@dataclass(frozen=True)
class Impuesto:
    """Un impuesto aplicable a un artículo."""

    impuesto_id: int
    nombre: str
    pctje: Decimal  # p.ej. Decimal("16.0")


@dataclass(frozen=True)
class PartidaCalculada:
    subtotal: Decimal   # precio × cantidad, precisión completa
    iva: Decimal        # IVA de la partida (suma de impuestos)
    total: Decimal      # subtotal + iva, redondeado a centavos


def _round_cents(value: Decimal) -> Decimal:
    """Redondea a centavos con ROUND_HALF_UP (el que usa Microsip)."""
    return value.quantize(CENTAVOS, rounding=ROUND_HALF_UP)


def precio_publico(precio_lista: Decimal, impuestos: list[Impuesto]) -> Decimal:
    """Precio unitario al público CON IVA incluido, redondeado a 2 decimales.

    Obligación legal (LFPC art. 7 Bis): el precio exhibido al consumidor final
    debe incluir impuestos. `precio_lista` viene sin IVA de la base; aquí se
    le suma la tasa de cada impuesto y se redondea a centavos.

    Ejemplo: precio_lista=100, IVA 16% → 116.00.
    """
    tasa_total = sum((imp.pctje / Decimal("100") for imp in impuestos), Decimal("0"))
    return _round_cents(precio_lista * (Decimal("1") + tasa_total))


def importe_partida(precio_publico: Decimal, cantidad: Decimal) -> Decimal:
    """Importe de una partida = precio público (con IVA) × cantidad, redondeado.

    Redondeo por partida: lo que el cliente ve (precio × cantidad) es exactamente
    lo que paga. Transparente para consumidor final.
    """
    return _round_cents(precio_publico * cantidad)


def calcular_iva_partida(
    precio_unitario: Decimal,
    cantidad: Decimal,
    impuestos: list[Impuesto],
) -> Decimal:
    """IVA total de una partida = suma de (precio × cantidad × tasa_i).

    Se calcula con precisión completa; el redondeo a centavos es del total.
    """
    base = precio_unitario * cantidad
    iva = Decimal("0")
    for imp in impuestos:
        tasa = imp.pctje / Decimal("100")
        iva += base * tasa
    return iva


def calcular_partida(
    precio_unitario: Decimal,
    cantidad: Decimal,
    impuestos: list[Impuesto],
) -> PartidaCalculada:
    """Calcula subtotal, IVA y total de una partida.

    - subtotal = precio × cantidad (precisión completa)
    - iva = subtotal × Σ tasas (precisión completa)
    - total = redondeo a centavos de (subtotal + iva)
    """
    subtotal = precio_unitario * cantidad
    iva = calcular_iva_partida(precio_unitario, cantidad, impuestos)
    total = _round_cents(subtotal + iva)
    return PartidaCalculada(subtotal=subtotal, iva=iva, total=total)


@dataclass
class Cotizacion:
    """Estructura de una cotización multi-partida."""

    partidas: list[dict] = field(default_factory=list)

    def agregar_partida(
        self,
        clave: str,
        nombre: str,
        cantidad: Decimal,
        precio_unitario: Decimal,
        impuestos: list[Impuesto],
    ) -> None:
        calc = calcular_partida(precio_unitario, cantidad, impuestos)
        self.partidas.append(
            {
                "clave": clave,
                "nombre": nombre,
                "cantidad": cantidad,
                "precio_unitario": precio_unitario,
                "impuestos": impuestos,
                "subtotal": calc.subtotal,
                "iva": calc.iva,
                "total": calc.total,
            }
        )

    def quitar_partida(self, indice: int) -> None:
        if 0 <= indice < len(self.partidas):
            self.partidas.pop(indice)

    def subtotal_total(self) -> Decimal:
        return sum((p["subtotal"] for p in self.partidas), Decimal("0"))

    def iva_total(self) -> Decimal:
        return sum((p["iva"] for p in self.partidas), Decimal("0"))

    def total(self) -> Decimal:
        """Total = redondeo a centavos de (subtotal + IVA)."""
        return _round_cents(self.subtotal_total() + self.iva_total())
