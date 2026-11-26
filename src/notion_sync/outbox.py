"""The outbox: queue writes, claim them, send them, back off on failure.

The outbox owns every retry. One attempt is one pass through `send`, and the
Notion SDK's own retries are off.
"""

import logging
import uuid
from collections import Counter
from datetime import timedelta

from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, connection, transaction
from django.db.models import Exists, F, OuterRef
from django.utils import timezone

from notion_sync import pages
from notion_sync.client import NotionClient
from notion_sync.conf import get_settings, get_target
from notion_sync.exceptions import (
    FATAL,
    UNCERTAIN,
    NotionAPIError,
    NotionValidationError,
)
from notion_sync.mapping import require_mapping
from notion_sync.models import OutboxItem
from notion_sync.records import link, write_page
from notion_sync.signals import send_safely, sync_failed
from notion_sync.values import decode_snapshot, encode_snapshot

logger = logging.getLogger("notion_sync")

Operation = OutboxItem.Operation
Status = OutboxItem.Status

BATCH_SIZE = 20
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600
MIN_RETRY_SECONDS = 1
ERROR_LIMIT = 2000

RETRYABLE_STATUSES = (Status.FAILED, Status.NEEDS_REVIEW)
SUPERSEDED = "A newer queued row for the same record replaced this one."

INTERRUPTED = (
    "Sending was interrupted and the page may or may not exist in Notion. "
    "Check the database before retrying."
)


class Skip(Exception):  # noqa: N818 - a control-flow signal, not an error
    """There is nothing to send for this row."""


def backoff_seconds(attempts):
    """Wait after the given number of failed attempts: 30s, 60s, 120s ... 1h."""
    return min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_MAX_SECONDS)


# --- Enqueue --------------------------------------------------------------


def enqueue(operation, target, properties, *, page_id="", external_id=None):
    """Store an outbox row. Makes no request to Notion.

    Values are checked for being storable here; they are checked against the
    Notion schema when the row is sent.
    """
    target = get_target(target)
    if external_id is not None:
        if not isinstance(external_id, str) or not external_id:
            raise NotionValidationError("external_id must be a non-empty string.")
        if not target.external_id_property:
            raise NotionValidationError(
                f"Target {target.name!r} has no EXTERNAL_ID_PROPERTY, "
                "so it cannot use an external ID."
            )
    if operation in (Operation.UPDATE, Operation.ARCHIVE) and not page_id:
        raise NotionValidationError(f"A queued {operation} needs a page ID.")
    if operation == Operation.UPSERT and external_id is None:
        raise NotionValidationError("A queued upsert needs an external ID.")
    item = OutboxItem.objects.create(
        target=target.name,
        operation=operation,
        page_id=page_id or "",
        external_id=external_id or "",
        payload=encode_snapshot(properties),
    )
    dispatch(item)
    return item


def dispatch(item):
    """With DISPATCH = "celery", send the row from a task once it commits.

    If the task cannot be queued, the row stays pending for the next drain.
    """
    if get_settings().dispatch != "celery":
        return
    pk = str(item.pk)

    def send_task():
        try:
            # Celery is optional: notion_sync.tasks imports it at module top.
            from notion_sync.tasks import send_outbox_item  # noqa: PLC0415

            send_outbox_item.delay(pk)
        except Exception:
            logger.warning(
                "Could not queue a Celery task for outbox item %s; "
                "the next drain will send it.",
                pk,
                exc_info=True,
            )

    transaction.on_commit(send_task)


# --- Claim ----------------------------------------------------------------


def claim(limit=BATCH_SIZE, *, exclude=(), only=None):
    """Mark up to `limit` due rows as sending and return them.

    Rows are locked with SKIP LOCKED where the database supports it, so
    several drains can run at once without taking the same row. A record's
    row waits while an earlier row for the same record is being sent or is
    parked for review, so two rows never race to create its page.
    """
    now = timezone.now()
    held = OutboxItem.objects.filter(
        content_type=OuterRef("content_type"),
        object_id=OuterRef("object_id"),
        status__in=(Status.SENDING, Status.NEEDS_REVIEW),
    )
    with transaction.atomic():
        due = (
            OutboxItem.objects.filter(status=Status.PENDING, next_attempt_at__lte=now)
            .exclude(pk__in=exclude)
            .filter(~Exists(held))
            .order_by("next_attempt_at", "created_at")
        )
        if only is not None:
            due = due.filter(pk__in=only)
        if connection.features.has_select_for_update:
            due = due.select_for_update(
                skip_locked=connection.features.has_select_for_update_skip_locked
            )
        items = list(due[:limit])
        OutboxItem.objects.filter(pk__in=[item.pk for item in items]).update(
            status=Status.SENDING,
            locked_at=now,
            attempts=F("attempts") + 1,
            updated_at=now,
        )
    for item in items:
        item.status = Status.SENDING
        item.locked_at = now
        item.attempts += 1
        item.updated_at = now
    return items


def release_stale():
    """Deal with rows left in `sending` by a drain that died.

    An interrupted create may have reached Notion. With an external ID
    property the next attempt looks the page up first, so the row goes back to
    pending. Without one it cannot be told apart from a duplicate, so the row
    is parked for a person to check. Returns the number of rows touched.
    """
    settings = get_settings()
    cutoff = timezone.now() - timedelta(seconds=settings.sending_timeout_seconds)
    stale = OutboxItem.objects.filter(status=Status.SENDING, locked_at__lt=cutoff)
    count = 0
    for item in stale:
        now = timezone.now()
        # Only touch the row if it is still stale: another drain may have
        # released it already.
        still_stale = OutboxItem.objects.filter(
            pk=item.pk, status=Status.SENDING, locked_at=item.locked_at
        )
        if may_duplicate(item, guard_target(item)):
            updated = still_stale.update(
                status=Status.NEEDS_REVIEW,
                locked_at=None,
                last_error=INTERRUPTED,
                updated_at=now,
            )
            if updated:
                item.refresh_from_db()
                send_safely(
                    sync_failed, sender=OutboxItem, item=item, error=INTERRUPTED
                )
        else:
            updated = return_to_pending(
                still_stale, item, locked_at=None, next_attempt_at=now, updated_at=now
            )
        count += updated
    return count


def guard_target(item):
    try:
        return get_target(item.target)
    except ImproperlyConfigured:
        return None


def may_duplicate(item, target):
    """Whether a lost response on this row could leave an unguarded page.

    Only a create can make a second page, and only a target with an external
    ID property can find the first one afterwards.
    """
    if target is not None and target.external_id_property:
        return False
    if item.operation == Operation.CREATE:
        return True
    if item.operation == Operation.SYNC:
        instance = load_instance(item)
        return instance is not None and not instance.notion_page_id
    return False


def return_to_pending(queryset, item, **changes):
    """Put rows back to pending; give way if the record has a newer pending row.

    A record has at most one pending row. If one was queued while this row
    was out, that newer row carries the latest values, so this one is skipped.
    """
    try:
        with transaction.atomic():
            return queryset.update(status=Status.PENDING, **changes)
    except IntegrityError:
        if item.content_type_id is None:
            raise
    for name in ("next_attempt_at", "last_error", "attempts"):
        changes.pop(name, None)
    return queryset.update(status=Status.SKIPPED, last_error=SUPERSEDED, **changes)


# --- Send -----------------------------------------------------------------


def load_instance(item):
    """The row's instance, or None if it was deleted."""
    model = item.content_type.model_class() if item.content_type_id else None
    if model is None:
        return None
    return model._base_manager.filter(pk=item.object_id).first()


def perform_sync(item, target, client):
    """Send a model sync row. Returns (page, operation, instance)."""
    instance = load_instance(item)
    if instance is None:
        raise Skip("The record was deleted before it was sent; nothing was written.")
    mapping = require_mapping(instance)
    page, operation = write_page(mapping, target, client, instance)
    link(instance, page)
    return page, operation, instance


def perform_item(item, target, client):
    """Send the row. Returns (page, operation, instance or None)."""
    operation = item.operation
    if operation == Operation.SYNC:
        return perform_sync(item, target, client)

    properties = decode_snapshot(item.payload)
    if operation == Operation.CREATE:
        if not target.external_id_property:
            return pages.create(target, client, properties), operation, None
        # The duplicate guard. Every queued create carries an ID, and an
        # attempt after a failed one looks for a page a lost response left.
        external_id = item.external_id or str(item.id)
        if item.attempts > 1 or item.last_error:
            existing = pages.find_by_external_id(target, client, external_id)
            if existing is not None:
                return pages.Page.from_notion(existing), operation, None
        page = pages.create(target, client, properties, external_id)
        return page, operation, None
    if operation == Operation.UPDATE:
        page = pages.update(target, client, item.page_id, properties)
        return page, operation, None
    if operation == Operation.UPSERT:
        page = pages.upsert(target, client, item.external_id, properties)
        return page, operation, None
    if operation == Operation.ARCHIVE:
        page_id = item.page_id
        if not page_id:
            # A deleted record whose page was never linked: find it by ID.
            if not target.external_id_property:
                raise Skip("No page is linked, so there is nothing to archive.")
            existing = pages.find_by_external_id(target, client, item.external_id)
            if existing is None:
                raise Skip(
                    f"No page has external ID {item.external_id!r}; nothing to archive."
                )
            page_id = existing["id"]
        return pages.archive(target, client, page_id), operation, None
    raise NotionValidationError(f"Unknown outbox operation {operation!r}.")


def save_fields(item, **changes):
    """Set fields on a row and save only those."""
    changes["updated_at"] = timezone.now()
    for name, value in changes.items():
        setattr(item, name, value)
    item.save(update_fields=list(changes))


def park_item(item, status, error):
    message = str(error)[:ERROR_LIMIT]
    save_fields(item, status=status, locked_at=None, last_error=message)
    logger.warning(
        "Outbox item %s (%s on %s) parked as %s: %s",
        item.pk,
        item.operation,
        item.target,
        status,
        message,
    )
    send_safely(sync_failed, sender=OutboxItem, item=item, error=message)


def reschedule_item(item, error, delay):
    now = timezone.now()
    return_to_pending(
        OutboxItem.objects.filter(pk=item.pk),
        item,
        locked_at=None,
        last_error=str(error)[:ERROR_LIMIT],
        next_attempt_at=now + timedelta(seconds=delay),
        updated_at=now,
    )
    item.refresh_from_db()


def send(item):
    """Send one claimed row and record the outcome on it."""
    settings = get_settings()
    target = None
    try:
        target = get_target(item.target)
        with NotionClient(target) as client:
            page, operation, instance = perform_item(item, target, client)
    except Skip as reason:
        save_fields(item, status=Status.SKIPPED, locked_at=None, last_error=str(reason))
    except (NotionValidationError, ImproperlyConfigured) as error:
        park_item(item, Status.FAILED, error)
    except NotionAPIError as error:
        if error.outcome == FATAL:
            park_item(item, Status.FAILED, error)
        elif error.outcome == UNCERTAIN and may_duplicate(item, target):
            park_item(item, Status.NEEDS_REVIEW, f"{error} {INTERRUPTED}")
        elif item.attempts >= settings.max_attempts:
            park_item(
                item, Status.FAILED, f"Gave up after {item.attempts} attempts: {error}"
            )
        else:
            delay = error.retry_after
            if delay is None:
                delay = backoff_seconds(item.attempts)
            reschedule_item(item, error, max(delay, MIN_RETRY_SECONDS))
    except Exception as error:
        logger.exception("Outbox item %s failed unexpectedly.", item.pk)
        park_item(item, Status.FAILED, f"Unexpected {type(error).__name__}: {error}")
    else:
        save_fields(
            item,
            status=Status.SUCCEEDED,
            locked_at=None,
            last_error="",
            result_page_id=page.id,
        )
        pages.send_page_synced(target, operation, page, item=item, instance=instance)
    return item


def drain_one(pk):
    """Send one row by primary key if it is pending and due. Returns it or None."""
    release_stale()
    for item in claim(1, only=[pk]):
        return send(item)
    return None


def drain(limit=None):
    """Send every due row, at most `limit` of them. Returns counts by status."""
    release_stale()
    counts: Counter[str] = Counter()
    seen: list[uuid.UUID] = []
    while limit is None or len(seen) < limit:
        size = BATCH_SIZE if limit is None else min(BATCH_SIZE, limit - len(seen))
        # A row rescheduled in this run is not picked up again until the next.
        batch = claim(size, exclude=seen)
        if not batch:
            break
        for item in batch:
            seen.append(item.pk)
            send(item)
            counts[item.status] += 1
    return counts


def retry(item):
    """Send a failed or parked row again, with a fresh set of attempts.

    The last error is kept, which also tells a queued create to look for a
    page an earlier attempt may have made. Returns True if the row is pending
    again, False if a newer row for the same record replaced it.
    """
    if item.status not in RETRYABLE_STATUSES:
        return False
    now = timezone.now()
    return_to_pending(
        OutboxItem.objects.filter(pk=item.pk, status=item.status),
        item,
        attempts=0,
        locked_at=None,
        next_attempt_at=now,
        updated_at=now,
    )
    item.refresh_from_db()
    if item.status != Status.PENDING:
        return False
    dispatch(item)
    return True


def prune(days=None):
    """Delete sent and skipped rows older than the retention period."""
    if days is None:
        days = get_settings().retention_days
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = OutboxItem.objects.filter(
        status__in=(Status.SUCCEEDED, Status.SKIPPED), updated_at__lt=cutoff
    ).delete()
    return deleted
