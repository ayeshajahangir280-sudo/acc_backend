from django.contrib import admin

from .models import Branch, BranchAccess, Membership, Organization


class BranchInline(admin.TabularInline):
    model = Branch
    extra = 0


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    autocomplete_fields = ["user"]


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "base_currency", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "legal_name")
    inlines = [BranchInline, MembershipInline]

    def has_delete_permission(self, request, obj=None):
        return False  # organizations own accounting history; deactivate instead


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "organization", "is_active")
    list_filter = ("is_active", "organization")
    search_fields = ("name", "code")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "role")
    list_filter = ("role", "organization")
    search_fields = ("user__username", "organization__name")
    autocomplete_fields = ["user"]


@admin.register(BranchAccess)
class BranchAccessAdmin(admin.ModelAdmin):
    list_display = ("membership", "branch")
    list_filter = ("branch__organization",)
    search_fields = ("membership__user__username", "branch__name")
