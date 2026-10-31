from django.contrib import admin

from apps.core.admin import PostedRecordAdmin

from .models import Voucher, VoucherLine
from .services import void_voucher


class LineInline(admin.TabularInline):
    model = VoucherLine
    extra = 0
    can_delete = False
    readonly_fields = ("account", "party", "debit", "credit", "description")

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Voucher)
class VoucherAdmin(PostedRecordAdmin):
    void_service = staticmethod(void_voucher)
    list_display = ("number", "type", "date", "total", "status", "organization")
    list_filter = ("type", "status", "organization")
    search_fields = ("number", "narration")
    date_hierarchy = "date"
    inlines = [LineInline]
