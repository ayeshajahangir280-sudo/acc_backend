"""Bank rules CRUD.

Same access rules as the reconciliation module: owners/admins only, never branch-restricted users."""
from rest_framework import serializers, viewsets
from rest_framework.exceptions import PermissionDenied

from apps.ledger.models import Account
from apps.organizations.access import scope_for_membership

from . import rules
from .models import BankRule
from .views import BankStatementViewSet


class BankRuleSerializer(serializers.ModelSerializer):
    account = serializers.PrimaryKeyRelatedField(queryset=Account.objects.all(), required=False, allow_null=True)
    category = serializers.PrimaryKeyRelatedField(queryset=Account.objects.all())
    category_name = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = BankRule
        fields = ["id", "name", "account", "direction", "contains", "category", "category_name", "tax_rate", "priority", "is_active"]


class BankRuleViewSet(viewsets.ModelViewSet):
    permission_classes = BankStatementViewSet.permission_classes
    write_roles = BankStatementViewSet.write_roles
    serializer_class = BankRuleSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]
    pagination_class = None
    queryset = BankRule.objects.select_related("category").all()

    def check_permissions(self, request):
        super().check_permissions(request)
        if scope_for_membership(request.membership) is not None:
            raise PermissionDenied("Bank rules are limited to owners and admins.")

    def get_queryset(self):
        return super().get_queryset().filter(organization=self.request.organization)

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "organization": self.request.organization}

    def _clean(self, d, inst=None):
        org = self.request.organization
        account = d.get("account", inst.account if inst else None)
        category = d.get("category", inst.category if inst else None)
        rules.check_rule(org, account=account, category=category, tax_rate=d.get("tax_rate", inst.tax_rate if inst else 0),
                         contains=d.get("contains", inst.contains if inst else ""))

    def perform_create(self, serializer):
        self._clean(serializer.validated_data)
        serializer.save(organization=self.request.organization)

    def perform_update(self, serializer):
        self._clean(serializer.validated_data, serializer.instance)
        serializer.save()
