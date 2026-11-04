import datetime
from decimal import Decimal, InvalidOperation

from django.db.models import Count
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.core.errors import DomainError
from apps.core.permissions import ADMIN_ROLES
from apps.core.views import OrgScopedMixin
from apps.ledger.models import Account
from apps.organizations.access import scope_for_membership

from . import services
from .models import BankStatement
from .serializers import BankStatementSerializer, MatchSerializer, StatementLineSerializer


def _dec(value, field):
    try:
        v = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise DomainError(f"{field} must be a number.", "invalid_amount")
    if not v.is_finite():
        raise DomainError(f"{field} must be a number.", "invalid_amount")
    return v.quantize(Decimal("0.01"))


def _int(value, field, default=None, lo=0, hi=366):
    if value in (None, ""):
        return default
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise DomainError(f"{field} must be a whole number.", "invalid_number")
    if not lo <= v <= hi:
        raise DomainError(f"{field} must be between {lo} and {hi}.", "invalid_number")
    return v


class BankStatementViewSet(OrgScopedMixin, viewsets.ReadOnlyModelViewSet):
    """Bank statements and their reconciliation. Never writes to the ledger.

    Bank accounts hold every branch's money, so this module is limited to owners/admins: a branch-restricted user must
    not see bank movements that belong to branches they cannot reach.
    """

    write_roles = ADMIN_ROLES
    queryset = BankStatement.objects.select_related("account", "reconciled_by").order_by("-period_end", "-id")
    serializer_class = BankStatementSerializer
    filterset_fields = ["account", "status"]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def check_permissions(self, request):
        super().check_permissions(request)
        if scope_for_membership(request.membership) is not None:
            raise PermissionDenied("Bank reconciliation is limited to owners and admins because bank accounts hold every branch's money.")

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.annotate(line_count=Count("lines")) if self.action == "list" else qs

    def _detail(self, st):
        data = BankStatementSerializer(st).data
        data["summary"] = services.summary(st)
        return data

    def retrieve(self, request, *args, **kwargs):
        return Response(self._detail(self.get_object()))

    @action(detail=False, methods=["post"], url_path="import")
    def import_(self, request):
        d = request.data
        try:
            account = Account.objects.get(pk=int(d.get("account")), organization=request.organization)
        except (TypeError, ValueError, Account.DoesNotExist):
            raise DomainError("Choose a bank or cash account.", "invalid_account")
        upload = request.FILES.get("file")
        if upload is None:
            raise DomainError("Send the statement as a CSV in the 'file' field.", "file_missing")
        st, info = services.import_statement(
            request.organization, request.user, account=account, raw=upload.read(), file_name=upload.name,
            opening_balance=_dec(d.get("opening_balance"), "opening_balance"), closing_balance=_dec(d.get("closing_balance"), "closing_balance"),
            date_format=d.get("date_format") or "iso", skip_duplicates=str(d.get("skip_duplicates", "")).lower() in ("1", "true", "yes"))
        return Response({**self._detail(st), **info}, status=201)

    @action(detail=True, methods=["get"])
    def lines(self, request, pk=None):
        st = self.get_object()
        qs = st.lines.prefetch_related("matches__journal_line__transaction")
        rows = StatementLineSerializer(qs, many=True).data
        wanted = request.query_params.get("status")
        return Response([r for r in rows if not wanted or r["status"] == wanted])

    @action(detail=True, methods=["get"])
    def candidates(self, request, pk=None):
        """Ledger entries that could clear a statement line (?line=ID), or every uncleared entry around the statement period."""
        st = self.get_object()
        days = _int(request.query_params.get("days"), "days", 30)
        line_id = request.query_params.get("line")
        line = None
        if line_id:
            line = st.lines.filter(pk=_int(line_id, "line", lo=1, hi=2**62)).first()
            if line is None:
                raise DomainError("Statement line not found.", "line_not_found")
        qs = services.ledger_candidates(st, sign=(1 if line.amount > 0 else -1) if line else 1, days=days, line_date=line.date if line else None)
        if line is None:
            qs = qs | services.ledger_candidates(st, sign=-1, days=days)
        out = [{"journal_line": c.id, "date": c.date, "reference": c.transaction.reference, "narration": c.transaction.narration,
                "source_type": c.transaction.source_type, "party": c.party.name if c.party_id else None,
                "amount": c.size, "remaining": c.remaining, "direction": "in" if c.debit > 0 else "out",
                "exact": bool(line) and c.remaining == abs(line.amount) - services.line_matched(line)} for c in qs.order_by("date", "id")[:200]]
        out.sort(key=lambda r: (not r["exact"],))
        return Response(out)

    @action(detail=True, methods=["post"])
    def match(self, request, pk=None):
        st = self.get_object()
        amount = request.data.get("amount")
        m = services.match(st, request.user, line_id=_int(request.data.get("line"), "line", lo=1, hi=2**62),
                           journal_line_id=_int(request.data.get("journal_line"), "journal_line", lo=1, hi=2**62),
                           amount=_dec(amount, "amount") if amount not in (None, "") else None)
        return Response(MatchSerializer(m).data, status=201)

    @action(detail=True, methods=["post"])
    def unmatch(self, request, pk=None):
        st = self.get_object()
        n = services.unmatch(st, match_id=_int(request.data.get("match"), "match", lo=1, hi=2**62) if request.data.get("match") else None,
                             line_id=_int(request.data.get("line"), "line", lo=1, hi=2**62) if request.data.get("line") else None)
        return Response({"removed": n})

    @action(detail=True, methods=["post"], url_path="auto-match")
    def auto_match(self, request, pk=None):
        return Response(services.auto_match(self.get_object(), request.user, _int(request.data.get("days"), "days", 7, 0, 60)))

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        return Response(self._detail(services.complete(self.get_object(), request.user)))

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        return Response(self._detail(services.reopen(self.get_object(), request.user)))

    @action(detail=True, methods=["post"])
    def discard(self, request, pk=None):
        services.discard(self.get_object())
        return Response(status=204)
