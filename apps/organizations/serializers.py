from django.utils import timezone
from rest_framework import serializers

from .models import Branch, BranchAccess, FiscalYearClose, Membership, Organization


class OrganizationSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = ["id", "name", "legal_name", "base_currency", "fiscal_year_start_month", "books_locked_through", "is_active", "role",
                  "expense_approval_required", "bill_approval_required", "approval_min_amount", "default_income_account"]
        read_only_fields = ["is_active"]

    def _mine(self, obj):
        user = self.context["request"].user
        return next((m for m in obj.memberships.all() if m.user_id == user.id and m.is_active), None)

    def get_role(self, obj):
        m = self._mine(obj)
        return m.role if m else None

    def validate_books_locked_through(self, v):
        if v is not None and v > timezone.localdate():
            raise serializers.ValidationError("The lock date cannot be in the future.")
        return v

    def validate_default_income_account(self, v):
        if v is None:
            return v
        from apps.ledger.income import is_eligible

        if self.instance is None or not is_eligible(self.instance, v):
            raise serializers.ValidationError("Choose an active income account of this organization (not Sales Returns).")
        return v

    def to_representation(self, obj):
        data = super().to_representation(obj)
        # Always tell clients which account new invoice lines default to (configured, or built-in Sales Revenue).
        from apps.ledger.income import default_income_account

        from apps.core import perms

        membership = None
        if "request" in self.context:
            membership = self._mine(obj)
            data["permissions"] = perms.as_lists(perms.effective(membership))
        try:
            acc = default_income_account(obj)
            data["effective_income_account"] = acc.id
        except Exception:  # organization without a chart yet
            data["effective_income_account"] = None
        if "request" in self.context and not perms.has(membership, "settings", "view"):
            for field in ("legal_name", "fiscal_year_start_month", "books_locked_through", "expense_approval_required",
                          "bill_approval_required", "approval_min_amount", "default_income_account", "effective_income_account"):
                data.pop(field, None)
        return data

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
    email = serializers.CharField(source="user.email", read_only=True)
    invited_by_name = serializers.CharField(source="invited_by.username", read_only=True, default=None)
    is_self = serializers.SerializerMethodField()

    class Meta:
        model = Membership
        fields = ["id", "user", "username", "email", "role", "is_active", "invited_by_name", "created_at", "is_self",
                  "custom_permissions", "permissions"]
        read_only_fields = fields

    custom_permissions = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()

    def get_custom_permissions(self, obj):
        return obj.role != "owner" and obj.permissions is not None

    def get_permissions(self, obj):
        from apps.core import perms

        return perms.as_lists(perms.effective(obj))

    def get_is_self(self, obj):
        request = self.context.get("request")
        return bool(request and obj.user_id == request.user.id)


class InvitationSerializer(serializers.ModelSerializer):
    invited_by_name = serializers.CharField(source="invited_by.username", read_only=True, default=None)
    status = serializers.CharField(read_only=True)

    class Meta:
        from .models import Invitation

        model = Invitation
        fields = ["id", "email", "role", "permissions", "status", "invited_by_name", "created_at", "expires_at", "accepted_at"]
        read_only_fields = fields
