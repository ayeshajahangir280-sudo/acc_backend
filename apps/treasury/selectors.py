"""Invoice-level outstanding balances, derived from allocations and returns (never stored)."""
from decimal import Decimal

from django.db.models import DecimalField, F, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from apps.purchases.models import PurchaseInvoice
from apps.sales.models import SalesInvoice

from .models import PaymentAllocation, ReceiptAllocation

_DEC = DecimalField(max_digits=18, decimal_places=2)
_ZERO = Value(Decimal("0"), output_field=_DEC)

# direction -> (invoice model, allocation model, allocation->document field)
KINDS = {
    "receipt": (SalesInvoice, ReceiptAllocation, "receipt"),
    "payment": (PurchaseInvoice, PaymentAllocation, "payment"),
}


def outstanding_invoices(org, direction, party=None, as_of=None, invoice_ids=None):
    """Posted invoices of `org` annotated with allocated / returned / outstanding (as of a date if given)."""
    inv, alloc, doc = KINDS[direction]
    a = alloc.objects.filter(invoice=OuterRef("pk"), **{f"{doc}__status": "posted"})
    r = inv.objects.filter(original=OuterRef("pk"), status="posted")
    if as_of:
        a = a.filter(**{f"{doc}__date__lte": as_of})
        r = r.filter(date__lte=as_of)
    a = a.values("invoice").annotate(s=Sum("amount")).values("s")
    r = r.values("original").annotate(s=Sum("total")).values("s")
    qs = inv.objects.filter(organization=org, kind="invoice", status="posted").annotate(
        allocated=Coalesce(Subquery(a, output_field=_DEC), _ZERO),
        returned=Coalesce(Subquery(r, output_field=_DEC), _ZERO),
    ).annotate(outstanding=F("total") - F("allocated") - F("returned"))
    if party:
        qs = qs.filter(party=party)
    if as_of:
        qs = qs.filter(date__lte=as_of)
    if invoice_ids is not None:
        qs = qs.filter(pk__in=invoice_ids)
    return qs
