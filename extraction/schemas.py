"""Structured extraction contract for the NEXT stage (OCR -> verified order).

Decimal fields are rounded consistently at validation time so downstream
Fakturama writes never see noisy precision (e.g. "250.0" vs "250.00000").
"""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field


_TWO_PLACES = Decimal("0.01")

def _to_decimal(value: object) -> Decimal | None:
    """Accept str/int/float/Decimal; strip currency noise; return None for blanks."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = value.strip().replace(" ", "").replace("\u00a0", "")
        if not cleaned:
            return None
        # Remove common currency symbols and percent sign; keep sign and dot.
        for junk in ("€", "$", "£", "EUR", "USD", "GBP", "%"):
            cleaned = cleaned.replace(junk, "")
        # If a value uses comma as decimal separator ("1.234,50" or "12,50"),
        # normalize to dot. Heuristic: if there's a comma and no dot after it,
        # treat the last comma as decimal separator.
        if "," in cleaned and "." not in cleaned:
            cleaned = cleaned.replace(",", ".")
        elif "," in cleaned and "." in cleaned:
            # "1,234.50" => drop thousands commas
            cleaned = cleaned.replace(",", "")
        return Decimal(cleaned)
    raise TypeError(f"Unsupported decimal value: {value!r}")


def _round(places: Decimal):
    def _validator(value: object) -> Decimal | None:
        d = _to_decimal(value)
        if d is None:
            return None
        return d.quantize(places, rounding=ROUND_HALF_UP)

    return _validator


# Reusable annotated Decimal types. The BeforeValidator runs on every assignment
# and every model_validate call, so numbers are always clean 2/3-dp Decimals.
Money = Annotated[Decimal, BeforeValidator(_round(_TWO_PLACES))]
Percent = Annotated[Decimal, BeforeValidator(_round(_TWO_PLACES))]
Qty = Annotated[Decimal, BeforeValidator(_round(_TWO_PLACES))]


class Address(BaseModel):
    street: str | None = None
    additional_name: str | None = None  # e.g. c/o, warehouse name line
    address_specification: str | None = None  # building, floor, apartment, etc.
    district: str | None = None
    zip_code: str | None = None  # string: preserves leading zeros
    city: str | None = None
    country: str | None = None
    email: str | None = None
    telephone: str | None = None


class Debtor(BaseModel):
    company: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    alias: str | None = None
    billing_address: Address = Field(default_factory=Address)
    delivery_address: Address | None = None


class Payment(BaseModel):
    method: str | None = None
    status: Literal["PAID", "UNPAID"] | None = None
    payment_date: date | None = None


class OrderItem(BaseModel):
    sku: str | None = None
    description: str | None = None
    quantity: Qty | None = None
    unit_net_price: Money | None = None
    vat_percent: Percent | None = None
    discount_percent: Percent | None = None
    source_line_net_total: Money | None = None


class OrderTotals(BaseModel):
    net: Money | None = None
    vat: Money | None = None
    gross: Money | None = None


class OrderData(BaseModel):
    order_date: date | None = None
    external_reference: str | None = None
    debtor: Debtor = Field(default_factory=Debtor)
    payment: Payment = Field(default_factory=Payment)
    items: list[OrderItem] = Field(default_factory=list)
    totals: OrderTotals = Field(default_factory=OrderTotals)
    order_discount_percent: Percent | None = None
    shipping_method: str | None = None
    shipping_value: Money | None = None
    # Do not invent any missing fields: stop for review if required data is absent.
