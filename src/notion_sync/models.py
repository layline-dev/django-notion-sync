import uuid

from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Q
from django.utils import timezone

from notion_sync.pages import Page

__all__ = ["NotionSyncMixin", "OutboxItem", "WebhookEvent"]


class OutboxItem(models.Model):
    """One queued write to Notion."""

    class Operation(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        UPSERT = "upsert", "Upsert"
        ARCHIVE = "archive", "Archive"
        # A model sync: creates or updates the instance's page, reading its
        # values when the row is sent.
        SYNC = "sync", "Sync"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENDING = "sending", "Sending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        NEEDS_REVIEW = "needs_review", "Needs review"
        # Nothing to send: the record was deleted or a newer row replaced it.
        SKIPPED = "skipped", "Skipped"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target = models.CharField(max_length=100)
    operation = models.CharField(max_length=10, choices=Operation.choices)
    page_id = models.CharField(max_length=36, blank=True)
    external_id = models.CharField(max_length=255, blank=True)
    payload = models.JSONField(default=dict, blank=True)

    # Set for model syncs, which read the instance's values when sent.
    content_type = models.ForeignKey(
        ContentType,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="+",
    )
    object_id = models.CharField(max_length=255, blank=True)

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING
    )
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    locked_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    result_page_id = models.CharField(max_length=36, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(
                fields=["status", "next_attempt_at"],
                name="notion_sync_outbox_due",
            ),
            models.Index(
                fields=["content_type", "object_id"],
                name="notion_sync_outbox_object",
            ),
        ]
        constraints = [
            # Saving a record again reuses its pending row, so a burst of
            # saves becomes one write that carries the latest values.
            models.UniqueConstraint(
                fields=["content_type", "object_id"],
                condition=Q(status="pending"),
                name="notion_sync_one_pending_per_object",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.operation} on {self.target} ({self.status})"


class WebhookEvent(models.Model):
    """A verified Notion webhook event, stored to be processed later.

    Used only with WEBHOOK_PROCESSING = "stored". Inline processing keeps
    seen event IDs in Django's cache instead.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        PROCESSED = "processed", "Processed"
        FAILED = "failed", "Failed"

    event_id = models.CharField(max_length=64, unique=True)
    type = models.CharField(max_length=100)
    entity_id = models.CharField(max_length=36)
    target = models.CharField(max_length=100)
    payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    locked_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    received_at = models.DateTimeField(default=timezone.now, editable=False)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["received_at"]
        indexes = [
            models.Index(
                fields=["status", "next_attempt_at"],
                name="notion_sync_event_due",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.type} {self.event_id} ({self.status})"


class NotionSyncMixin(models.Model):
    """Adds the fields that link a model instance to its Notion page.

    Register a `NotionMapping` for the model to say what to send. The host
    project's own migration adds these fields to its table.
    """

    # NULL, not "", for "no page", so a host can add a unique constraint.
    notion_page_id = models.CharField(  # noqa: DJ001
        max_length=36, null=True, blank=True, db_index=True, editable=False
    )
    notion_synced_at = models.DateTimeField(null=True, blank=True, editable=False)
    notion_last_edited_time = models.DateTimeField(
        null=True, blank=True, editable=False
    )

    class Meta:
        abstract = True

    # sync imports OutboxItem from this module, so these methods import sync
    # when called.

    def sync_to_notion(self) -> Page:
        """Create or update this record's page now and return the `Page`.

        Raises on failure; nothing is retried.
        """
        from notion_sync.sync import sync_instance  # noqa: PLC0415

        return sync_instance(self)

    def enqueue_notion_sync(self) -> "OutboxItem":
        """Queue a sync of this record and return the `OutboxItem`.

        If the record already has a pending row, that row is returned and no
        new one is made. Values are read when the row is sent.
        """
        from notion_sync.sync import enqueue_instance  # noqa: PLC0415

        return enqueue_instance(self)
