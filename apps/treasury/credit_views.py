"""Credit-application endpoints, mixed into the sales and purchase invoice viewsets."""
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.ledger.models import Account
from apps.organizations.access import request_scope

from .models import PaymentMethod

from . import credits
from .selectors import outstanding_invoices


class _ApplyIn(serializers.Serializer):
    date = serializers.DateField(required=False)
    applications = serializers.ListField(child=serializers.DictField(), allow_empty=False)


class _RefundIn(serializers.Serializer):
    cash_account = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=18, decimal_places=2)
    date = serializers.DateField(required=False)
    method = serializers.ChoiceField(choices=[c for c, _ in PaymentMethod.choices], required=False)
    reference = serializers.CharField(max_length=100, required=False, allow_blank=True)
    narration = serializers.CharField(max_length=500, required=False, allow_blank=True)
    idempotency_key = serializers.CharField(max_length=64, required=False, allow_null=True)


def _refund_row(r):
    return {"id": r.id, "number": r.number, "date": r.date, "amount": r.amount, "status": r.status, "cash_account": r.cash_account_id,
            "method": r.method, "reference": r.reference, "invoice": r.invoice_id}


class CreditActionsMixin:
    credit_direction = None  # "receipt" (sales) or "payment" (purchases)

    def _visible_invoice(self, pk):
        """Resolve through the branch-scoped queryset so hidden documents behave as not found."""
        qs = self.get_queryset().filter(kind="invoice", status="posted")
        obj = qs.filter(pk=pk).first() if str(pk).isdigit() else None
        if obj is None:
            raise serializers.ValidationError({"invoice": "Invoice not found."})
        return obj

    @action(detail=False, methods=["get"], url_path="credits")
    def credit_list(self, request):
        """Invoices of one party (or all) that hold unapplied credit."""
        rows = credits.credit_invoices(request.organization, self.credit_direction, party=request.query_params.get("party"),
                                       branch=request_scope(request))
        return Response([{"id": r.id, "number": r.number, "date": r.date, "party": r.party_id, "branch": r.branch_id,
                          "total": r.total, "outstanding": r.outstanding, "credit_available": credits.credit_available(r)} for r in rows])

    @action(detail=True, methods=["get"], url_path="credit")
    def credit(self, request, pk=None):
        inv = self.get_object()
        return Response(credits.credit_summary(request.organization, self.credit_direction, inv.pk))

    @action(detail=True, methods=["post"], url_path="apply-credit")
    def apply_credit(self, request, pk=None):
        src = self.get_object()
        ser = _ApplyIn(data=request.data)
        ser.is_valid(raise_exception=True)
        apps = []
        for item in ser.validated_data["applications"]:
            try:
                target_id, amount = item["invoice"], item["amount"]
            except KeyError:
                raise serializers.ValidationError({"applications": "Each item needs invoice and amount."})
            apps.append({"invoice": self._visible_invoice(target_id), "amount": amount})
        out = credits.apply_credit(request.organization, request.user, self.credit_direction, src, apps, ser.validated_data.get("date"))
        return Response(out)

    @action(detail=True, methods=["get", "post"], url_path="refunds")
    def refunds(self, request, pk=None):
        """GET: refunds of this invoice. POST: refund (part of) the credit it holds, through a cash/bank account."""
        inv = self.get_object()
        model = credits.REFUND_MODELS[self.credit_direction]
        if request.method == "GET":
            return Response([_refund_row(r) for r in model.objects.filter(invoice=inv, organization=request.organization).order_by("id")])
        ser = _RefundIn(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data
        acct = Account.objects.filter(pk=v["cash_account"], organization=request.organization).first()
        if acct is None:
            raise serializers.ValidationError({"cash_account": "Not found."})
        refund, created = credits.refund_credit(
            request.organization, request.user, self.credit_direction, inv, cash_account=acct, amount=v["amount"], date=v.get("date"),
            method=v.get("method", "cash"), reference=v.get("reference", ""), narration=v.get("narration", ""),
            idempotency_key=v.get("idempotency_key"))
        return Response(_refund_row(refund), status=201 if created else 200)

    @action(detail=True, methods=["post"], url_path="void-refund")
    def void_refund(self, request, pk=None):
        inv = self.get_object()
        rid = request.data.get("refund")
        model = credits.REFUND_MODELS[self.credit_direction]
        if not str(rid).isdigit() or not model.objects.filter(pk=rid, invoice=inv, organization=request.organization).exists():
            raise serializers.ValidationError({"refund": "Refund not found on this invoice."})
        return Response(_refund_row(credits.void_refund(request.organization, request.user, self.credit_direction, int(rid))))

    @action(detail=True, methods=["post"], url_path="unapply-credit")
    def unapply_credit(self, request, pk=None):
        inv = self.get_object()
        app_id = request.data.get("application")
        if not str(app_id).isdigit():
            raise serializers.ValidationError({"application": "Required."})
        model = credits.CREDIT_MODELS[self.credit_direction]
        from django.db.models import Q

        if not model.objects.filter(Q(from_invoice=inv) | Q(to_invoice=inv), pk=app_id, organization=request.organization).exists():
            raise serializers.ValidationError({"application": "Credit application not found on this invoice."})
        credits.unapply_credit(request.organization, self.credit_direction, int(app_id))
        return Response(credits.credit_summary(request.organization, self.credit_direction, inv.pk))
