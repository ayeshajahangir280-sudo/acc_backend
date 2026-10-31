from rest_framework import serializers

from apps.organizations.models import Branch
from apps.parties.models import Party

from .models import SalesInvoice, SalesItem


class SalesItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = SalesItem
        fields = ["id", "description", "quantity", "unit_price", "tax_rate", "net", "tax", "gross"]
        read_only_fields = ["net", "tax", "gross"]


class SalesInvoiceListSerializer(serializers.ModelSerializer):
    party_name = serializers.CharField(source="party.name", read_only=True)

    class Meta:
        model = SalesInvoice
        fields = ["id", "number", "kind", "date", "party", "party_name", "original", "due_date", "subtotal", "tax_total", "total",
                  "status", "narration"]


class SalesInvoiceSerializer(SalesInvoiceListSerializer):
    items = SalesItemSerializer(many=True)
    party = serializers.PrimaryKeyRelatedField(queryset=Party.objects.all())
    original = serializers.PrimaryKeyRelatedField(queryset=SalesInvoice.objects.all(), required=False, allow_null=True)
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all(), required=False, allow_null=True)
    idempotency_key = serializers.CharField(max_length=64, required=False, allow_null=True, write_only=True)

    class Meta(SalesInvoiceListSerializer.Meta):
        fields = SalesInvoiceListSerializer.Meta.fields + ["items", "branch", "idempotency_key"]
        read_only_fields = ["number", "subtotal", "tax_total", "total", "status"]

    def validate(self, attrs):
        # Cross-tenant guard: related objects must belong to the caller's organization.
        org = self.context["organization"]
        for key in ("party", "original", "branch"):
            obj = attrs.get(key)
            if obj is not None and obj.organization_id != org.id:
                raise serializers.ValidationError({key: "Not found."})
        return attrs
