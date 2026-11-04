from django.contrib import admin

from .models import BankStatement


@admin.register(BankStatement)
class BankStatementAdmin(admin.ModelAdmin):
    list_display = ("id", "organization", "account", "period_start", "period_end", "status")
    list_filter = ("status", "organization")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
