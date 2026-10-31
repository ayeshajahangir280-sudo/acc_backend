"""Shared decimal helpers for document line math. Rates are supplied by the user/config, never hardcoded."""
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def line_amounts(quantity, unit_price, tax_rate):
    """Returns (net, tax, gross) for one line. tax_rate is a percentage, e.g. Decimal('5')."""
    net = money(Decimal(quantity) * Decimal(unit_price))
    tax = money(net * Decimal(tax_rate) / Decimal(100))
    return net, tax, net + tax
