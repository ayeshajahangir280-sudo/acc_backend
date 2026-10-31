from django.contrib import admin

from apps.core.admin import PostedRecordAdmin

from .models import Account, JournalLine, JournalTransaction
from .services import void_journal


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "type", "organization", "is_cash_bank", "is_active")
    list_filter = ("type", "is_cash_bank", "is_active", "organization")
    search_fields = ("code", "name")
    readonly_fields = ("system_code",)

    def has_delete_permission(self, request, obj=None):
        return False


class LineInline(admin.TabularInline):
    model = JournalLine
    extra = 0
    can_delete = False
    readonly_fields = ("account", "party", "debit", "credit", "description", "date")

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(JournalTransaction)
class JournalTransactionAdmin(PostedRecordAdmin):
    list_display = ("id", "date", "source_type", "reference", "status", "organization")
    list_filter = ("status", "source_type", "organization")
    search_fields = ("reference", "narration")
    date_hierarchy = "date"
    inlines = [LineInline]
    actions = []  # documents are voided from their own admin page so the document status stays in sync
