from rest_framework import serializers

from apps.ledger.models import Account
from apps.organizations.models import Branch
from apps.parties.models import Party

from . import services
from .models import RecurringDocument, RecurringExpense, RecurringItem


class RecurringItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecurringItem
        fields = ["id", "description", "quantity", "unit_price", "tax_rate"]


class RecurringSerializer(serializers.ModelSerializer):
    items = RecurringItemSerializer(many=True)
    party = serializers.PrimaryKeyRelatedField(queryset=Party.objects.all())
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all(), required=False, allow_null=True)
    party_name = serializers.CharField(source="party.name", read_only=True)
    next_date = serializers.SerializerMethodField()

    class Meta:
        model = RecurringDocument
        fields = ["id", "side", "name", "party", "party_name", "branch", "narration", "frequency", "interval", "start_date",
                  "end_date", "due_days", "generated", "is_active", "last_run_on", "last_error", "next_date", "items"]
        read_only_fields = ["generated", "is_active", "last_run_on", "last_error"]

    def get_next_date(self, obj):
        d = services.next_date(obj)
        return d.isoformat() if d else None

    def validate(self, attrs):
        org = self.context["organization"]
        for key in ("party", "branch"):
            obj = attrs.get(key)
            if obj is not None and obj.organization_id != org.id:
                raise serializers.ValidationError({key: "Not found."})
        return attrs


class RecurringExpenseSerializer(serializers.ModelSerializer):
    expense_account = serializers.PrimaryKeyRelatedField(queryset=Account.objects.all())
    paid_through = serializers.PrimaryKeyRelatedField(queryset=Account.objects.all())
    vendor = serializers.PrimaryKeyRelatedField(queryset=Party.objects.all(), required=False, allow_null=True)
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all(), required=False, allow_null=True)
    expense_account_name = serializers.CharField(source="expense_account.name", read_only=True)
    paid_through_name = serializers.CharField(source="paid_through.name", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True, default=None)
    next_date = serializers.SerializerMethodField()

    class Meta:
        model = RecurringExpense
        fields = ["id", "name", "expense_account", "expense_account_name", "paid_through", "paid_through_name", "vendor",
                  "vendor_name", "amount", "tax_rate", "amount_includes_tax", "branch", "reference", "narration", "frequency",
                  "interval", "start_date", "end_date", "generated", "is_active", "last_run_on", "last_error", "next_date"]
        read_only_fields = ["generated", "is_active", "last_run_on", "last_error"]

    def get_next_date(self, obj):
        d = services.next_date(obj)
        return d.isoformat() if d else None

    def validate(self, attrs):
        org = self.context["organization"]
        for key in ("expense_account", "paid_through", "vendor", "branch"):
            obj = attrs.get(key)
            if obj is not None and obj.organization_id != org.id:
                raise serializers.ValidationError({key: "Not found."})
        return attrs
