from rest_framework import serializers

from apps.ledger.models import Account
from apps.organizations.models import Branch
from apps.parties.models import Party

from .models import Expense


class ExpenseListSerializer(serializers.ModelSerializer):
    expense_account_name = serializers.CharField(source="expense_account.name", read_only=True)
    paid_through_name = serializers.CharField(source="paid_through.name", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True, default=None)
    has_receipt = serializers.SerializerMethodField()

    class Meta:
        model = Expense
        fields = ["id", "number", "date", "expense_account", "expense_account_name", "paid_through", "paid_through_name", "vendor",
                  "vendor_name", "reference", "tax_rate", "amount_includes_tax", "subtotal", "tax_total", "total", "status",
                  "narration", "branch", "has_receipt"]

    def get_has_receipt(self, obj):
        return bool(obj.receipt)


class ExpenseSerializer(ExpenseListSerializer):
    expense_account = serializers.PrimaryKeyRelatedField(queryset=Account.objects.all())
    paid_through = serializers.PrimaryKeyRelatedField(queryset=Account.objects.all())
    vendor = serializers.PrimaryKeyRelatedField(queryset=Party.objects.all(), required=False, allow_null=True)
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all(), required=False, allow_null=True)
    amount = serializers.DecimalField(max_digits=18, decimal_places=2, write_only=True)
    idempotency_key = serializers.CharField(max_length=64, required=False, allow_null=True, write_only=True)

    class Meta(ExpenseListSerializer.Meta):
        fields = ExpenseListSerializer.Meta.fields + ["amount", "idempotency_key"]
        read_only_fields = ["number", "subtotal", "tax_total", "total", "status"]

    def validate(self, attrs):
        org = self.context["organization"]
        for key in ("expense_account", "paid_through", "vendor", "branch"):
            obj = attrs.get(key)
            if obj is not None and obj.organization_id != org.id:
                raise serializers.ValidationError({key: "Not found."})
        return attrs
