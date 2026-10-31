from rest_framework import serializers

from apps.organizations.models import Branch
from apps.parties.models import Party

from .models import PurchaseInvoice, PurchaseItem


class PurchaseItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = PurchaseItem
        fields = ["id", "description", "quantity", "unit_price", "tax_rate", "net", "tax", "gross"]
        read_only_fields = ["net", "tax", "gross"]


class PurchaseInvoiceListSerializer(serializers.ModelSerializer):
    party_name = serializers.CharField(source="party.name", read_only=True)

    class Meta:
        model = PurchaseInvoice
        fields = ["id", "number", "kind", "date", "party", "party_name", "original", "due_date", "subtotal", "tax_total", "total",
                  "status", "narration"]


class PurchaseInvoiceSerializer(PurchaseInvoiceListSerializer):
    items = PurchaseItemSerializer(many=True)
    party = serializers.PrimaryKeyRelatedField(queryset=Party.objects.all())
    original = serializers.PrimaryKeyRelatedField(queryset=PurchaseInvoice.objects.all(), required=False, allow_null=True)
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all(), required=False, allow_null=True)
    idempotency_key = serializers.CharField(max_length=64, required=False, allow_null=True, write_only=True)

    class Meta(PurchaseInvoiceListSerializer.Meta):
        fields = PurchaseInvoiceListSerializer.Meta.fields + ["items", "branch", "idempotency_key"]
        read_only_fields = ["number", "subtotal", "tax_total", "total", "status"]

    def validate(self, attrs):
        # Cross-tenant guard: related objects must belong to the caller's organization.
        org = self.context["organization"]
        for key in ("party", "original", "branch"):
            obj = attrs.get(key)
            if obj is not None and obj.organization_id != org.id:
                raise serializers.ValidationError({key: "Not found."})
        return attrs

