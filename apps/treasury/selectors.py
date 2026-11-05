"""Invoice-level outstanding balances, derived from allocations and returns (never stored)."""
from decimal import Decimal

from django.db.models import DecimalField, F, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from apps.purchases.models import PurchaseInvoice
from apps.sales.models import SalesInvoice

from .models import (PaymentAllocation, PurchaseCreditApplication, PurchaseCreditRefund, ReceiptAllocation, SalesCreditApplication,
                     SalesCreditRefund)

_DEC = DecimalField(max_digits=18, decimal_places=2)
_ZERO = Value(Decimal("0"), output_field=_DEC)

# direction -> (invoice model, allocation model, allocation->document field)
KINDS = {
    "receipt": (SalesInvoice, ReceiptAllocation, "receipt"),
    "payment": (PurchaseInvoice, PaymentAllocation, "payment"),
}
CREDIT_MODELS = {"receipt": SalesCreditApplication, "payment": PurchaseCreditApplication}
REFUND_MODELS = {"receipt": SalesCreditRefund, "payment": PurchaseCreditRefund}


def outstanding_invoices(org, direction, party=None, as_of=None, invoice_ids=None, branch=None):
    """Posted invoices of `org` annotated with allocated / returned / outstanding (as of a date if given)."""
    inv, alloc, doc = KINDS[direction]
    a = alloc.objects.filter(invoice=OuterRef("pk"), **{f"{doc}__status": "posted"})
    r = inv.objects.filter(original=OuterRef("pk"), status="posted")
    if as_of:
        a = a.filter(**{f"{doc}__date__lte": as_of})
        r = r.filter(date__lte=as_of)
    cm = CREDIT_MODELS[direction]
    cin = cm.objects.filter(to_invoice=OuterRef("pk"))
    cout = cm.objects.filter(from_invoice=OuterRef("pk"))
    if as_of:
        cin, cout = cin.filter(date__lte=as_of), cout.filter(date__lte=as_of)
    rf = REFUND_MODELS[direction].objects.filter(invoice=OuterRef("pk"), status="posted")
    if as_of:
        rf = rf.filter(date__lte=as_of)
    rf = rf.values("invoice").annotate(s=Sum("amount")).values("s")
    a = a.values("invoice").annotate(s=Sum("amount")).values("s")
    r = r.values("original").annotate(s=Sum("total")).values("s")
    cin = cin.values("to_invoice").annotate(s=Sum("amount")).values("s")
    cout = cout.values("from_invoice").annotate(s=Sum("amount")).values("s")
    qs = inv.objects.filter(organization=org, kind="invoice", status="posted").annotate(
        allocated=Coalesce(Subquery(a, output_field=_DEC), _ZERO),
        returned=Coalesce(Subquery(r, output_field=_DEC), _ZERO),
        applied_in=Coalesce(Subquery(cin, output_field=_DEC), _ZERO),
        applied_out=Coalesce(Subquery(cout, output_field=_DEC), _ZERO),
        refunded=Coalesce(Subquery(rf, output_field=_DEC), _ZERO),
    ).annotate(outstanding=F("total") - F("allocated") - F("returned") - F("applied_in") + F("applied_out") + F("refunded"))
    if party:
        qs = qs.filter(party=party)
    if as_of:
        qs = qs.filter(date__lte=as_of)
    if invoice_ids is not None:
        qs = qs.filter(pk__in=invoice_ids)
    if branch is not None:  # None = all branches; an iterable of ids (possibly empty) restricts to those
        qs = qs.filter(branch_id__in=[branch] if isinstance(branch, int) else list(branch))
    return qs
