from django.contrib import admin, messages

from .errors import DomainError

admin.site.site_header = "Accounting administration"
admin.site.site_title = "Accounting admin"


class PostedRecordAdmin(admin.ModelAdmin):
    """Admin for posted accounting records: browse, search, and void only.

    Posted records are immutable (no add/change/delete) so ledger integrity cannot be bypassed from the admin.
    Voiding goes through the same service as the API, which posts a reversing entry.
    """

    void_service = None  # callable(obj, user)
    actions = ["void_selected"]
    list_per_page = 50
    show_full_result_count = False

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    @admin.action(description="Void selected (posts reversing entries)")
    def void_selected(self, request, queryset):
        done = 0
        for obj in queryset:
            try:
                type(self).void_service(obj, request.user)
                done += 1
            except DomainError as e:
                self.message_user(request, f"{obj}: {e.message}", messages.ERROR)
        if done:
            self.message_user(request, f"Voided {done} record(s).", messages.SUCCESS)
