from datetime import date

from django.utils import timezone

from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import IsOrgMember
from apps.organizations.access import report_branch

from . import selectors


def _date(request, name, default=None):
    raw = request.query_params.get(name)
    if not raw:
        return default
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValidationError({name: "Use YYYY-MM-DD."})


def _int(request, name):
    raw = request.query_params.get(name)
    if not raw:
        return None
    if not raw.isdigit():
        raise ValidationError({name: "Must be a number."})
    return int(raw)


def _range(request):
    start, end = _date(request, "date_from"), _date(request, "date_to")
    if start and end and start > end:
        raise ValidationError({"date_from": "date_from cannot be after date_to."})
    return start, end


class _Report(APIView):
    """Read-only; every role in the organization may view reports. The organization comes from the header."""

    permission_classes = [IsOrgMember]
    throttle_classes = []


class TrialBalanceView(_Report):
    def get(self, request):
        return Response(selectors.trial_balance(request.organization, _date(request, "as_of"), report_branch(request)))


class ProfitLossView(_Report):
    def get(self, request):
        start, end = _range(request)
        return Response(selectors.profit_and_loss(request.organization, start, end, report_branch(request)))


class BalanceSheetView(_Report):
    def get(self, request):
        return Response(selectors.balance_sheet(request.organization, _date(request, "as_of"), report_branch(request)))


class AgingView(_Report):
    def get(self, request):
        kind = request.query_params.get("type", "receivable")
        if kind not in ("receivable", "payable"):
            raise ValidationError({"type": "Use receivable or payable."})
        raw = request.query_params.get("buckets", "30,60,90")
        try:
            bounds = tuple(int(x) for x in raw.split(","))
        except ValueError:
            raise ValidationError({"buckets": "Comma-separated whole numbers, e.g. 30,60,90."})
        if not 1 <= len(bounds) <= 5 or bounds[0] < 1 or list(bounds) != sorted(set(bounds)):
            raise ValidationError({"buckets": "Between 1 and 5 strictly increasing positive numbers."})
        as_of = _date(request, "as_of", timezone.localdate())
        return Response(selectors.aging(request.organization, "receipt" if kind == "receivable" else "payment",
                                        as_of, _int(request, "party"), bounds, report_branch(request)))


class VendorReportView(_Report):
    def get(self, request):
        start, end = _range(request)
        return Response(selectors.vendor_report(request.organization, start, end, _int(request, "party"), report_branch(request)))


class SalesSummaryView(_Report):
    def get(self, request):
        group = request.query_params.get("group_by", "month")
        if group not in ("day", "month", "customer"):
            raise ValidationError({"group_by": "Use day, month or customer."})
        start, end = _range(request)
        return Response(selectors.sales_summary(request.organization, start, end, _int(request, "party"), group, report_branch(request)))


class VatSummaryView(_Report):
    def get(self, request):
        start, end = _range(request)
        return Response(selectors.vat_summary(request.organization, start, end, report_branch(request)))


class VatReturnView(_Report):
    def get(self, request):
        start, end = _range(request)
        return Response(selectors.vat_return(request.organization, start, end, report_branch(request)))
