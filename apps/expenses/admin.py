from django.contrib import admin

from apps.core.admin import PostedRecordAdmin

from .models import Expense
from .services import void_expense


@admin.register(Expense)
class ExpenseAdmin(PostedRecordAdmin):
    void_service = staticmethod(void_expense)
    list_display = ("number", "date", "expense_account", "total", "status", "organization")
    list_filter = ("status", "organization")
    search_fields = ("number", "reference", "narration")
    date_hierarchy = "date"
