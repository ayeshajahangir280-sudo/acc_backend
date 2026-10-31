from rest_framework import serializers

from .models import Account, JournalLine, JournalTransaction


class AccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = Account
        fields = ["id", "code", "name", "type", "system_code", "is_cash_bank", "is_active"]
        read_only_fields = ["system_code"]
        validators = []

    def validate_code(self, v):
        qs = Account.objects.filter(organization=self.context["organization"], code=v)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("Account code already exists.")
        return v

    def create(self, validated_data):
        return Account.objects.create(organization=self.context["organization"], **validated_data)


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
