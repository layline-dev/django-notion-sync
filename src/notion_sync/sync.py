"""Syncing model instances: inline, queued, and the save and delete hooks."""

import contextlib
import contextvars
import copy
import logging

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.db.models.fields.files import FieldFile

from notion_sync import outbox, pages
from notion_sync.client import NotionClient
from notion_sync.conf import get_target
from notion_sync.exceptions import NotionValidationError
from notion_sync.mapping import MIXIN_FIELDS, get_mapping, require_mapping
from notion_sync.models import OutboxItem
from notion_sync.records import INITIAL_VALUES, link, write_page

logger = logging.getLogger("notion_sync")

Operation = OutboxItem.Operation
Status = OutboxItem.Status

# Rows that may still lead to a write, so a delete must clean up after them.
UNFINISHED = (Status.PENDING, Status.SENDING, Status.FAILED, Status.NEEDS_REVIEW)

_DEFERRED = object()

# The (concrete model, pk) of a record being deleted because its page went to
# the trash in Notion. That page is already in the trash, so the delete hook
# queues no archive for it. Records removed by a cascade are not covered.
_trashed_in_notion = contextvars.ContextVar("notion_sync_trashed", default=None)


def instance_key(instance):
    return (instance._meta.concrete_model, str(instance.pk))


@contextlib.contextmanager
def deleting_for_notion_trash(instance):
    token = _trashed_in_notion.set(instance_key(instance))
    try:
        yield
    finally:
        _trashed_in_notion.reset(token)


# --- Sending ----------------------------------------------------------------


def require_saved(instance):
    if instance.pk is None:
        raise NotionValidationError(
            "Save the record before syncing it to Notion; it has no primary key."
        )


def sync_instance(instance):
    """Create or update an instance's page now. Returns the `Page`."""
    require_saved(instance)
    mapping = require_mapping(instance)
    target = get_target(mapping.target)
    with NotionClient(target) as client:
        page, operation = write_page(mapping, target, client, instance)
    link(instance, page)
    pages.send_page_synced(target, operation, page, instance=instance)
    return page


# --- Queueing ---------------------------------------------------------------


def content_type_for(instance):
    return ContentType.objects.get_for_model(instance, for_concrete_model=False)


def enqueue_instance(instance, *, operation=Operation.SYNC, page_id="", external_id=""):
    """Queue a write for an instance, reusing its pending row if it has one."""
    require_saved(instance)
    mapping = require_mapping(instance)
    content_type = content_type_for(instance)
    object_id = str(instance.pk)
    wanted = {"operation": operation, "page_id": page_id, "external_id": external_id}

    with transaction.atomic():
        pending = OutboxItem.objects.filter(
            content_type=content_type, object_id=object_id, status=Status.PENDING
        )
        item = pending.select_for_update().first()
        if item is None:
            try:
                with transaction.atomic():
                    item = OutboxItem.objects.create(
                        target=mapping.target,
                        content_type=content_type,
                        object_id=object_id,
                        **wanted,
                    )
            except IntegrityError:
                # Another transaction queued the same record a moment ago.
                item = pending.select_for_update().get()
        changes = {k: v for k, v in wanted.items() if getattr(item, k) != v}
        if changes:
            # The row's job changed, e.g. a sync turned into an archive.
            changes.update(attempts=0, target=mapping.target)
            outbox.save_fields(item, **changes)
    outbox.dispatch(item)
    return item


# --- Save and delete hooks ------------------------------------------------------


def snapshot_value(value):
    if isinstance(value, FieldFile):
        return value.name
    if isinstance(value, dict | list):
        return copy.deepcopy(value)
    return value


def remember_initial(instance, attnames):
    instance.__dict__[INITIAL_VALUES] = {
        name: snapshot_value(instance.__dict__.get(name, _DEFERRED))
        for name in attnames
    }


def concrete_attnames(instance):
    return [field.attname for field in instance._meta.concrete_fields]


def remember_values(sender, instance, **kwargs):
    """post_init: note field values, to tell later whether a save changed any."""
    mapping = get_mapping(instance)
    if mapping is None:
        return
    watched = mapping.watched()
    attnames = concrete_attnames(instance) if watched is None else watched
    remember_initial(instance, attnames)


def watched_changes(mapping, instance, update_fields):
    watched = mapping.watched()
    initial = instance.__dict__.get(INITIAL_VALUES, {})
    if update_fields is not None:
        # Writes of only Notion-owned or link fields are not outbound changes.
        ignored = mapping.inbound_attnames() | MIXIN_FIELDS
        considered = {
            instance._meta.get_field(name).attname for name in update_fields
        } - ignored
        if watched is not None:
            considered &= watched
        return bool(considered)
    if watched is None:
        return True
    for name in watched:
        current = instance.__dict__.get(name, _DEFERRED)
        if current is _DEFERRED:
            continue  # Not loaded, so this save did not change it.
        if initial.get(name, _DEFERRED) != snapshot_value(current):
            return True
    return False


def after_save(sender, instance, created, raw, update_fields, **kwargs):
    """post_save: queue a sync if auto_sync is on and an Out field changed."""
    mapping = get_mapping(instance)
    if mapping is None or raw or not mapping.outbound:
        return
    try:
        if not mapping.get_auto_sync():
            return
        if created or watched_changes(mapping, instance, update_fields):
            enqueue_instance(instance)
    finally:
        watched = mapping.watched()
        remember_initial(
            instance, concrete_attnames(instance) if watched is None else watched
        )


def after_delete(sender, instance, **kwargs):
    """post_delete: queue an archive of the page if on_delete is "archive".

    A pending sync row for the record turns into the archive. Without one,
    the sync row is skipped when it comes up, as the record is gone.
    """
    mapping = get_mapping(instance)
    if mapping is None or mapping.get_on_delete() != "archive":
        return
    if _trashed_in_notion.get() == instance_key(instance):
        return
    page_id = instance.notion_page_id or ""
    external_id = ""
    if not page_id:
        # No page is linked, but an unfinished row may have created one that
        # was never linked. Archive by external ID, which finds it if so.
        unfinished = OutboxItem.objects.filter(
            content_type=content_type_for(instance),
            object_id=str(instance.pk),
            status__in=UNFINISHED,
        )
        if not unfinished.exists():
            return
        external_id = mapping.get_external_id(instance)
    enqueue_instance(
        instance,
        operation=Operation.ARCHIVE,
        page_id=page_id,
        external_id=external_id,
    )
