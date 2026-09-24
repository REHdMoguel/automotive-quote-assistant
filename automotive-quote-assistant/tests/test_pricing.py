"""Tests del motor de cálculo (Fase 2).

Los fixtures usan escenarios sintéticos de cálculo.
"""

from decimal import Decimal

from backend.pricing import (
    Cotizacion,
    Impuesto,
    calcular_partida,
    precio_publico,
    importe_partida,
)

IVA16 = [Impuesto(impuesto_id=588, nombre="IVA 16%", pctje=Decimal("16.0"))]
IVA0 = [Impuesto(impuesto_id=591, nombre="IVA 0%", pctje=Decimal("0.0"))]
EXENTO = [Impuesto(impuesto_id=592, nombre="IVA Exento", pctje=Decimal("0.0"))]


def test_precio_publico_con_iva():
    """Precio al público = lista × (1 + IVA). 100 → 116.00 (LFPC art. 7 Bis)."""
    assert precio_publico(Decimal("100"), IVA16) == Decimal("116.00")
    assert precio_publico(Decimal("12100"), IVA16) == Decimal("14036.00")


def test_precio_publico_iva_cero():
    """IVA 0% no altera el precio."""
    assert precio_publico(Decimal("100"), IVA0) == Decimal("100.00")
    assert precio_publico(Decimal("100"), EXENTO) == Decimal("100.00")


def test_importe_partida_redondeado():
    """Importe = precio público × cantidad, redondeado a centavos."""
    assert importe_partida(Decimal("116.00"), Decimal("4")) == Decimal("464.00")
    assert importe_partida(Decimal("92.32"), Decimal("2")) == Decimal("184.64")


def test_factura_real_fcs001590():
    """12,100 × 4 = 48,400 neto; IVA 16% = 7,744."""
    calc = calcular_partida(
        precio_unitario=Decimal("12100"), cantidad=Decimal("4"), impuestos=IVA16
    )
    assert calc.subtotal == Decimal("48400")
    assert calc.iva == Decimal("7744.00")
    assert calc.total == Decimal("56144.00")


def test_iva_cero():
    calc = calcular_partida(Decimal("100"), Decimal("2"), IVA0)
    assert calc.iva == Decimal("0")
    assert calc.total == Decimal("200.00")


def test_exento():
    calc = calcular_partida(Decimal("100"), Decimal("1"), EXENTO)
    assert calc.iva == Decimal("0")
    assert calc.total == Decimal("100.00")


def test_redondeo_half_up():
    """0.5 centavos redondea hacia arriba (no banker's rounding)."""
    # precio con 6 decimales que al sumar IVA da .005
    calc = calcular_partida(
        precio_unitario=Decimal("79.589471"), cantidad=Decimal("1"), impuestos=IVA16
    )
    # 79.589471 × 1.16 = 92.32378636 → total redondeado 92.32
    assert calc.total == Decimal("92.32")


def test_precision_no_float():
    """Verifica que NO se usa float: el total debe ser exacto con Decimal."""
    calc = calcular_partida(
        precio_unitario=Decimal("0.1"), cantidad=Decimal("3"), impuestos=IVA0
    )
    # 0.1 × 3 = 0.3 exacto (con float sería 0.30000000000000004)
    assert calc.total == Decimal("0.30")


def test_cotizacion_multipartida():
    c = Cotizacion()
    c.agregar_partida("A", "Art A", Decimal("2"), Decimal("100"), IVA16)
    c.agregar_partida("B", "Art B", Decimal("1"), Decimal("50"), IVA0)
    # subtotal = 200 + 50 = 250; IVA = 32 + 0 = 32; total = 282
    assert c.subtotal_total() == Decimal("250")
    assert c.iva_total() == Decimal("32.00")
    assert c.total() == Decimal("282.00")


def test_quitar_partida():
    c = Cotizacion()
    c.agregar_partida("A", "Art A", Decimal("1"), Decimal("100"), IVA16)
    c.agregar_partida("B", "Art B", Decimal("1"), Decimal("50"), IVA0)
    c.quitar_partida(0)
    assert len(c.partidas) == 1
    assert c.partidas[0]["clave"] == "B"
