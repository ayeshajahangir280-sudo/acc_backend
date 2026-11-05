from django.utils import timezone
from rest_framework import serializers

from .models import Branch, BranchAccess, FiscalYearClose, Membership, Organization


class OrganizationSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = ["id", "name", "legal_name", "base_currency", "fiscal_year_start_month", "books_locked_through", "is_active", "role",
                  "expense_approval_required", "bill_approval_required", "approval_min_amount"]
        read_only_fields = ["is_active"]

    def get_role(self, obj):
        user = self.context["request"].user
        return next((m.role for m in obj.memberships.all() if m.user_id == user.id), None)

    def validate_books_locked_through(self, v):
        if v is not None and v > timezone.localdate():
            raise serializers.ValidationError("The lock date cannot be in the future.")
        return v

    def validate_fiscal_year_start_month(self, v):
        if not 1 <= v <= 12:
            raise serializers.ValidationError("Must be 1-12.")
        return v


class FiscalYearCloseSerializer(serializers.ModelSerializer):
    closed_by_name = serializers.CharField(source="closed_by.username", read_only=True, default=None)

    class Meta:
        model = FiscalYearClose
        fields = ["id", "year_start", "year_end", "net_profit", "previous_lock", "closed_by_name", "created_at", "reopened_at", "reopen_reason"]


class BranchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branch
        fields = ["id", "name", "code", "is_active"]

    def create(self, validated_data):
        return Branch.objects.create(organization=self.context["organization"], **validated_data)


class BranchAccessSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="membership.user.username", read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    membership = serializers.PrimaryKeyRelatedField(queryset=Membership.objects.all())
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all())

    class Meta:
        model = BranchAccess
        fields = ["id", "membership", "username", "branch", "branch_name"]

    def validate(self, attrs):
        org = self.context["organization"]
        if attrs["membership"].organization_id != org.id:
            raise serializers.ValidationError({"membership": "Not found."})
        if attrs["branch"].organization_id != org.id:
            raise serializers.ValidationError({"branch": "Not found."})
        if BranchAccess.objects.filter(membership=attrs["membership"], branch=attrs["branch"]).exists():
            raise serializers.ValidationError("This member already has access to that branch.")
        return attrs


class MembershipSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = Membership
        fields = ["id", "user", "username", "role"]
