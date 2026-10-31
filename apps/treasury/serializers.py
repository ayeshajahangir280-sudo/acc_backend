from decimal import Decimal

from rest_framework import serializers

from apps.ledger.models import Account
from apps.organizations.models import Branch
from apps.parties.models import Party

from apps.purchases.models import PurchaseInvoice
from apps.sales.models import SalesInvoice

from .models import Payment, Receipt

FIELDS = ["id", "number", "date", "party", "party_name", "counter_account", "cash_account", "amount", "method",
          "reference", "narration", "status", "branch", "allocations", "allocated", "unapplied"]


def _allocation_serializer(invoice_model):
    class _Alloc(serializers.Serializer):
        invoice = serializers.PrimaryKeyRelatedField(queryset=invoice_model.objects.all())
        amount = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=Decimal("0.01"))
        invoice_number = serializers.CharField(source="invoice.number", read_only=True)

    return _Alloc


class _Base(serializers.ModelSerializer):
    allocated = serializers.SerializerMethodField()
    unapplied = serializers.SerializerMethodField()

    def get_allocated(self, obj):
        return sum((a.amount for a in obj.allocations.all()), obj.amount * 0)

    def get_unapplied(self, obj):
        return obj.amount - self.get_allocated(obj)

    party_name = serializers.CharField(source="party.name", read_only=True, default=None)
    party = serializers.PrimaryKeyRelatedField(queryset=Party.objects.all(), required=False, allow_null=True)
    counter_account = serializers.PrimaryKeyRelatedField(queryset=Account.objects.all(), required=False, allow_null=True)
    cash_account = serializers.PrimaryKeyRelatedField(queryset=Account.objects.all())
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all(), required=False, allow_null=True)
    idempotency_key = serializers.CharField(max_length=64, required=False, allow_null=True, write_only=True)

    def validate(self, attrs):
        org = self.context["organization"]
        for a in attrs.get("allocations", []):
            if a["invoice"].organization_id != org.id:
                raise serializers.ValidationError({"allocations": "Invoice not found."})
        for key in ("party", "counter_account", "cash_account", "branch"):
            obj = attrs.get(key)
            if obj is not None and obj.organization_id != org.id:
                raise serializers.ValidationError({key: "Not found."})
        return attrs


class ReceiptSerializer(_Base):
    allocations = _allocation_serializer(SalesInvoice)(many=True, required=False)

    class Meta:
        model = Receipt
        fields = FIELDS + ["idempotency_key"]
        read_only_fields = ["number", "status"]


class PaymentSerializer(_Base):
    allocations = _allocation_serializer(PurchaseInvoice)(many=True, required=False)

    class Meta:
        model = Payment
        fields = FIELDS + ["idempotency_key"]
        read_only_fields = ["number", "status"]
