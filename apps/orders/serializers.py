from rest_framework import serializers

from apps.inventory.models import Item
from apps.organizations.models import Branch
from apps.parties.models import Party

from .models import PurchaseOrder, PurchaseOrderItem, SalesOrder, SalesOrderItem


class _ItemSer(serializers.ModelSerializer):
    class Meta:
        fields = ["id", "description", "quantity", "unit_price", "tax_rate", "net", "tax", "gross", "invoiced_qty", "remaining_qty", "item"]
        read_only_fields = ["net", "tax", "gross", "invoiced_qty"]

    item = serializers.PrimaryKeyRelatedField(queryset=Item.objects.all(), required=False, allow_null=True)

    remaining_qty = serializers.SerializerMethodField()

    def get_remaining_qty(self, obj):
        return str(obj.quantity - obj.invoiced_qty)


class SalesOrderItemSerializer(_ItemSer):
    class Meta(_ItemSer.Meta):
        model = SalesOrderItem


class PurchaseOrderItemSerializer(_ItemSer):
    class Meta(_ItemSer.Meta):
        model = PurchaseOrderItem


class _OrderSer(serializers.ModelSerializer):
    party_name = serializers.CharField(source="party.name", read_only=True)
    party = serializers.PrimaryKeyRelatedField(queryset=Party.objects.all())
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all(), required=False, allow_null=True)
    idempotency_key = serializers.CharField(max_length=64, required=False, allow_null=True, write_only=True)
    invoice_number = serializers.CharField(source="invoice.number", read_only=True, default=None)

    BASE = ["id", "number", "date", "valid_until", "party", "party_name", "branch", "narration", "status", "subtotal",
            "tax_total", "total", "invoice", "invoice_number", "invoices"]
    invoices = serializers.SerializerMethodField()

    def get_invoices(self, obj):
        return [{"id": c.invoice_id, "number": c.invoice.number} for c in obj.conversions.all()]

    def validate(self, attrs):
        org = self.context["organization"]
        for key in ("party", "branch"):
            obj = attrs.get(key)
            if obj is not None and obj.organization_id != org.id:
                raise serializers.ValidationError({key: "Not found."})
        return attrs


class SalesOrderSerializer(_OrderSer):
    items = SalesOrderItemSerializer(many=True)
    kind = serializers.ChoiceField(choices=SalesOrder.Kind.choices)

    class Meta:
        model = SalesOrder
        fields = _OrderSer.BASE + ["kind", "source", "items", "idempotency_key"]
        read_only_fields = ["number", "subtotal", "tax_total", "total", "status", "invoice", "source"]


class PurchaseOrderSerializer(_OrderSer):
    items = PurchaseOrderItemSerializer(many=True)

    class Meta:
        model = PurchaseOrder
        fields = _OrderSer.BASE + ["items", "idempotency_key"]
        read_only_fields = ["number", "subtotal", "tax_total", "total", "status", "invoice"]


class ConvertLineSerializer(serializers.Serializer):
    item = serializers.IntegerField()
    quantity = serializers.DecimalField(max_digits=18, decimal_places=4)
