from rest_framework import serializers

from . import hierarchy
from .models import Account, JournalLine, JournalTransaction


class AccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = Account
        fields = ["id", "code", "name", "type", "system_code", "subtype", "is_cash_bank", "is_active", "parent", "parent_name"]
        read_only_fields = ["system_code"]
        validators = []

    parent = serializers.PrimaryKeyRelatedField(queryset=Account.objects.all(), required=False, allow_null=True)
    parent_name = serializers.CharField(source="parent.name", read_only=True, default=None)

    def validate_code(self, v):
        qs = Account.objects.filter(organization=self.context["organization"], code=v)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("Account code already exists.")
        return v

    def validate(self, attrs):
        inst = self.instance
        if inst is not None and inst.system_code:
            # Posting services find these by system_code, so their type and active state are fixed; name/code/subtype are editable.
            if "type" in attrs and attrs["type"] != inst.type:
                raise serializers.ValidationError({"type": "The type of a built-in account cannot be changed."})
            if attrs.get("is_active") is False:
                raise serializers.ValidationError({"is_active": "A built-in account cannot be deactivated."})
        return attrs

    def create(self, validated_data):
        return hierarchy.create_account(self.context["organization"], **validated_data)

    def update(self, instance, validated_data):
        return hierarchy.update_account(self.context["organization"], instance, **validated_data)


class JournalLineSerializer(serializers.ModelSerializer):
    account_name = serializers.CharField(source="account.name", read_only=True)

    class Meta:
        model = JournalLine
        fields = ["id", "account", "account_name", "party", "debit", "credit", "description"]


class JournalTransactionSerializer(serializers.ModelSerializer):
    lines = JournalLineSerializer(many=True, read_only=True)

    class Meta:
        model = JournalTransaction
        fields = ["id", "date", "source_type", "source_id", "reference", "narration", "status", "lines"]
