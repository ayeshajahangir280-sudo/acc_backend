from django.contrib import admin

from .models import Party


@admin.register(Party)
class PartyAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "organization", "is_active")
    list_filter = ("kind", "is_active", "organization")
    search_fields = ("name", "email", "phone", "tax_number")
    list_select_related = ("organization",)

    def has_delete_permission(self, request, obj=None):
        return False
