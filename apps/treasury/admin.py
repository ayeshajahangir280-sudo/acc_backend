from django.contrib import admin

from apps.core.admin import PostedRecordAdmin

from .models import Payment, PaymentAllocation, Receipt, ReceiptAllocation
from .services import void_cash_document


def _alloc_inline(model):
    class Inline(admin.TabularInline):
        extra = 0
        can_delete = False
        readonly_fields = ("invoice", "amount")

        def has_add_permission(self, request, obj=None):
            return False

        def has_change_permission(self, request, obj=None):
            return False

    Inline.model = model
    return Inline


class _CashAdmin(PostedRecordAdmin):
    void_service = staticmethod(void_cash_document)
    list_display = ("number", "date", "party", "amount", "method", "status", "organization")
    list_filter = ("method", "status", "organization")
    search_fields = ("number", "reference", "party__name")
    date_hierarchy = "date"
    list_select_related = ("party", "organization")


@admin.register(Receipt)
class ReceiptAdmin(_CashAdmin):
    inlines = [_alloc_inline(ReceiptAllocation)]


@admin.register(Payment)
class PaymentAdmin(_CashAdmin):
    inlines = [_alloc_inline(PaymentAllocation)]
