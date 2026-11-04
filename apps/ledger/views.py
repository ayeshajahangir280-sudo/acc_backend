from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.views import OrgReadOnlyViewSet, OrgScopedMixin
from apps.parties.models import Party

from . import selectors
from .models import Account, JournalTransaction
from .serializers import AccountSerializer, JournalTransactionSerializer


class AccountViewSet(OrgScopedMixin, viewsets.ModelViewSet):
    queryset = Account.objects.all()
    serializer_class = AccountSerializer
    filterset_fields = ["type", "is_active", "is_cash_bank"]
    search_fields = ["code", "name"]
    ordering = ["code"]
    http_method_names = ["get", "post", "patch", "head", "options"]
    pagination_class = None  # chart of accounts is small and used for dropdowns

    @action(detail=False, methods=["get"])
    def balances(self, request):
        from apps.organizations.access import request_scope

        bal = selectors.account_balances(request.organization, request.query_params.get("as_of"), request_scope(request))
        return Response(
            [
                {"account": a.id, "code": a.code, "name": a.name, "type": a.type, "is_cash_bank": a.is_cash_bank,
                 "debit": bal.get(a.id, (0, 0))[0], "credit": bal.get(a.id, (0, 0))[1]}
                for a in self.get_queryset().order_by("code")
            ]
        )


class JournalViewSet(OrgReadOnlyViewSet):
    branch_scoped = True
    queryset = JournalTransaction.objects.prefetch_related("lines__account").order_by("-date", "-id")
    serializer_class = JournalTransactionSerializer
    filterset_fields = {"date": ["gte", "lte"], "source_type": ["exact"], "status": ["exact"]}
    search_fields = ["reference", "narration"]


class PartyBalanceViewSet(OrgScopedMixin, viewsets.ViewSet):
    def list(self, request):
        from apps.organizations.access import request_scope

        bal = selectors.party_balances(request.organization, as_of=request.query_params.get("as_of"), branch=request_scope(request))
        parties = Party.objects.filter(organization=request.organization, id__in=bal.keys()).order_by("name")
        return Response(
            [{"party": p.id, "name": p.name, "kind": p.kind, **bal[p.id]} for p in parties]
        )
