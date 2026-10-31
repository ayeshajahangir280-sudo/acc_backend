from django.contrib import admin

from apps.core.admin import PostedRecordAdmin

from .models import PurchaseInvoice, PurchaseItem
from .services import void_document


class ItemInline(admin.TabularInline):
    model = PurchaseItem
    extra = 0
    can_delete = False
    readonly_fields = ("description", "quantity", "unit_price", "tax_rate", "net", "tax", "gross")

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(PurchaseInvoice)
class PurchaseInvoiceAdmin(PostedRecordAdmin):
    void_service = staticmethod(void_document)
    list_display = ("number", "kind", "date", "party", "total", "status", "organization")
    list_filter = ("kind", "status", "organization")
    search_fields = ("number", "party__name", "narration")
    date_hierarchy = "date"
    list_select_related = ("party", "organization")
    inlines = [ItemInline]
