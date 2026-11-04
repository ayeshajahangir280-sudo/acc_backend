from rest_framework import serializers

from apps.core.models import DocStatus

from .models import BankStatement, StatementLine, StatementMatch
from .services import line_status


class MatchSerializer(serializers.ModelSerializer):
    reference = serializers.CharField(source="journal_line.transaction.reference", read_only=True)
    ledger_date = serializers.DateField(source="journal_line.date", read_only=True)
    source_type = serializers.CharField(source="journal_line.transaction.source_type", read_only=True)
    stale = serializers.SerializerMethodField()

    class Meta:
        model = StatementMatch
        fields = ["id", "journal_line", "amount", "reference", "ledger_date", "source_type", "method", "stale"]

    def get_stale(self, obj):
        return obj.journal_line.transaction.status != DocStatus.POSTED  # the matched transaction was voided later


class StatementLineSerializer(serializers.ModelSerializer):
    matches = MatchSerializer(many=True, read_only=True)
    matched_amount = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()

    class Meta:
        model = StatementLine
        fields = ["id", "row", "date", "description", "reference", "amount", "matched_amount", "status", "matches"]

    def _eff(self, obj):
        return sum((m.amount for m in obj.matches.all() if m.journal_line.transaction.status == DocStatus.POSTED), 0)

    def get_matched_amount(self, obj):
        return self._eff(obj)

    def get_status(self, obj):
        return line_status(obj, self._eff(obj))


class BankStatementSerializer(serializers.ModelSerializer):
    account_name = serializers.CharField(source="account.name", read_only=True)
    reconciled_by_name = serializers.CharField(source="reconciled_by.username", read_only=True, default=None)
    line_count = serializers.IntegerField(read_only=True, default=None)

    class Meta:
        model = BankStatement
        fields = ["id", "account", "account_name", "file_name", "period_start", "period_end", "opening_balance", "closing_balance",
                  "status", "reconciled_by_name", "reconciled_at", "created_at", "line_count"]
