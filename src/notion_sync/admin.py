import json

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.template.response import TemplateResponse
from django.urls import path
from django.utils.html import format_html
from django.utils.text import Truncator

from notion_sync import outbox, webhooks
from notion_sync.models import OutboxItem, WebhookEvent


@admin.register(OutboxItem)
class OutboxItemAdmin(admin.ModelAdmin):
    list_display = [
        "created_at",
        "target",
        "operation",
        "status",
        "attempts",
        "next_attempt_at",
        "record",
        "short_error",
    ]
    list_filter = ["status", "target", "operation"]
    search_fields = [
        "=id",
        "=page_id",
        "=result_page_id",
        "=external_id",
        "=object_id",
        "last_error",
    ]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]
    actions = ["retry"]
    fields = (
        "id",
        "target",
        "operation",
        "status",
        "attempts",
        "next_attempt_at",
        "locked_at",
        "page_id",
        "external_id",
        "result_page_id",
        "record",
        "last_error",
        "pretty_payload",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    @admin.display(description="Record")
    def record(self, item):
        if not item.content_type_id:
            return "-"
        return f"{item.content_type.app_labeled_name} {item.object_id}"

    @admin.display(description="Last error")
    def short_error(self, item):
        return Truncator(item.last_error).chars(80)

    @admin.display(description="Payload")
    def pretty_payload(self, item):
        if not item.payload:
            return "(none: values are read from the record when sent)"
        text = json.dumps(item.payload, indent=2, ensure_ascii=False, sort_keys=True)
        return format_html("<pre>{}</pre>", text)

    @admin.action(description="Retry selected failed rows", permissions=["change"])
    def retry(self, request, queryset):
        retried = replaced = ignored = 0
        for item in queryset:
            if item.status not in outbox.RETRYABLE_STATUSES:
                ignored += 1
            elif outbox.retry(item):
                retried += 1
            else:
                replaced += 1
        if retried:
            self.message_user(request, f"{retried} row(s) queued to send again.")
        if replaced:
            self.message_user(
                request,
                f"{replaced} row(s) skipped: a newer row for the same record is "
                "already queued.",
                messages.WARNING,
            )
        if ignored:
            self.message_user(
                request,
                f"{ignored} row(s) left alone: only failed or needs-review rows "
                "can be retried.",
                messages.WARNING,
            )


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    """Stored webhook events, read-only, and the handshake token page."""

    change_list_template = "admin/notion_sync/webhookevent/change_list.html"

    list_display = [
        "received_at",
        "type",
        "target",
        "status",
        "attempts",
        "entity_id",
        "short_error",
    ]
    list_filter = ["status", "target", "type"]
    search_fields = ["=event_id", "=entity_id", "error"]
    date_hierarchy = "received_at"
    ordering = ["-received_at"]
    fields = (
        "event_id",
        "type",
        "target",
        "entity_id",
        "status",
        "attempts",
        "next_attempt_at",
        "locked_at",
        "error",
        "pretty_payload",
        "received_at",
        "processed_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Last error")
    def short_error(self, event):
        return Truncator(event.error).chars(80)

    @admin.display(description="Payload")
    def pretty_payload(self, event):
        text = json.dumps(event.payload, indent=2, ensure_ascii=False, sort_keys=True)
        return format_html("<pre>{}</pre>", text)

    # --- The subscription handshake token ---

    def get_urls(self):
        # admin_view() requires a staff login and sends never-cache headers.
        view = self.admin_site.admin_view(self.verification_token_view)
        return [
            path(
                "verification-token/",
                view,
                name="notion_sync_webhookevent_verification_token",
            ),
            *super().get_urls(),
        ]

    def changelist_view(self, request, extra_context=None):
        extra_context = {
            **(extra_context or {}),
            "show_verification_token_link": request.user.is_superuser,
        }
        return super().changelist_view(request, extra_context)

    def verification_token_view(self, request):
        """Show tokens from Notion's handshake. Superusers only.

        The token is read from the cache, where the webhook view put it for 15
        minutes. It is shown, never stored anywhere else or logged.
        """
        if not request.user.is_superuser:
            raise PermissionDenied
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Webhook verification token",
            "tokens": webhooks.recent_verification_tokens(),
            "minutes": webhooks.HANDSHAKE_SECONDS // 60,
        }
        return TemplateResponse(
            request, "admin/notion_sync/webhookevent/verification_token.html", context
        )
