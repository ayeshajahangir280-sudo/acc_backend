from rest_framework import serializers

from apps.ledger.models import Account
from apps.organizations.models import Branch
from apps.parties.models import Party

from .models import Voucher, VoucherLine


class VoucherLineSerializer(serializers.ModelSerializer):
    account = serializers.PrimaryKeyRelatedField(queryset=Account.objects.all())
    party = serializers.PrimaryKeyRelatedField(queryset=Party.objects.all(), required=False, allow_null=True)

    class Meta:
        model = VoucherLine
        fields = ["id", "account", "party", "debit", "credit", "description"]


class VoucherListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Voucher
        fields = ["id", "number", "type", "date", "total", "status", "narration"]


class VoucherSerializer(VoucherListSerializer):
    lines = VoucherLineSerializer(many=True)
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all(), required=False, allow_null=True)
    idempotency_key = serializers.CharField(max_length=64, required=False, allow_null=True, write_only=True)

    class Meta(VoucherListSerializer.Meta):
        fields = VoucherListSerializer.Meta.fields + ["lines", "branch", "idempotency_key"]
        read_only_fields = ["number", "total", "status"]

    def validate(self, attrs):
        org = self.context["organization"]
        objs = [attrs.get("branch")] + [x for l in attrs["lines"] for x in (l["account"], l.get("party"))]
        if any(o is not None and o.organization_id != org.id for o in objs):
            raise serializers.ValidationError("Referenced account, party or branch not found.")
        return attrs
