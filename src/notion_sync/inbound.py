"""Applying Notion's state to linked records: shared by webhooks and reconcile.

Webhook events carry IDs, not values, so a page is always fetched first and
its current state applied. Applying is idempotent:

- A state older than the one already applied to a record is skipped. Notion
  reports edit times to the minute, so an equal time cannot be ordered: a
  state with an equal time that would change a field is read again while
  the record is locked, and that fresh read is applied. A second edit in the
  same minute is not lost, and an older copy fetched by a slower worker
  cannot overwrite a newer one.
- Only `In` fields and `notion_last_edited_time` are written, with
  `save(update_fields=...)`, which never queues an outbound sync. An echo of
  the package's own write therefore changes nothing.
"""

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from django.conf import settings as django_settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from notion_sync import pages
from notion_sync.client import NotionClient
from notion_sync.conf import get_settings, get_target
from notion_sync.exceptions import NotionNotFoundError
from notion_sync.mapping import NotionMapping, registered
from notion_sync.signals import page_changed, page_trashed, send_safely
from notion_sync.sync import deleting_for_notion_trash
from notion_sync.values import DateRange

logger = logging.getLogger("notion_sync")

TRASH_EVENT = "page.deleted"
RECONCILE = "reconcile"
QUERY_PAGE_SIZE = 100


def normalize_id(value):
    """Notion IDs with or without dashes compare equal after this."""
    return str(value or "").replace("-", "").lower()


# --- Values -------------------------------------------------------------------


class UnusableValue(Exception):  # noqa: N818 - a control-flow signal
    pass


TEXT_FIELDS = ("CharField", "TextField", "SlugField")


def field_value(field, value, instance):
    """Turn a plain Notion value into one the model field can store."""
    kind = field.get_internal_type()
    if isinstance(value, DateRange):
        value = value.start
    if value is None:
        if field.null:
            return None
        if field.empty_strings_allowed:
            value = ""
        elif kind == "BooleanField":
            value = False
        else:
            raise UnusableValue("the property is empty and the field is not nullable")
    if kind == "DateTimeField" and type(value) is date:
        value = datetime(value.year, value.month, value.day)
    if (
        kind == "DateTimeField"
        and isinstance(value, datetime)
        and timezone.is_naive(value)
        and django_settings.USE_TZ
    ):
        value = timezone.make_aware(value)
    if (
        kind in TEXT_FIELDS
        and isinstance(value, int | float)
        and not isinstance(value, bool)
    ):
        value = str(value)  # A unique ID without a prefix, say.
    try:
        return field.clean(value, instance)
    except ValidationError as error:
        raise UnusableValue("; ".join(error.messages)) from None


def inbound_values(mapping, page, instance):
    """{attname: value} for each In field the page has a usable value for."""
    values = {}
    for name, spec in mapping.inbound.items():
        if name not in page.properties:
            logger.warning(
                'Page %s has no property "%s" (mapped by %s); left unchanged.',
                page.id,
                name,
                type(mapping).__name__,
            )
            continue
        field = mapping._concrete_field(spec.field)
        try:
            values[field.attname] = field_value(field, page.properties[name], instance)
        except UnusableValue as reason:
            logger.warning(
                'Property "%s" of page %s does not fit %s.%s (%s); left unchanged.',
                name,
                page.id,
                mapping.model.__name__,
                field.name,
                reason,
            )
    return values


# --- Applying a page -------------------------------------------------------------


def mappings_for(target):
    return [mapping for mapping in registered() if mapping.target == target.name]


def linked_records(target, page_id):
    """[(mapping, pk)] of records linked to the page, across mapped models."""
    found: list[tuple[NotionMapping, Any]] = []
    for mapping in mappings_for(target):
        pks = mapping.model._base_manager.filter(notion_page_id=page_id).values_list(
            "pk", flat=True
        )
        found.extend((mapping, pk) for pk in pks)
    return found


def compare_state(mapping, instance, page):
    """(stale, changed attnames, values, advanced) for a page and a record."""
    stored = instance.notion_last_edited_time
    edited = page.last_edited_time
    if stored is not None and edited is not None and edited < stored:
        return True, [], {}, False
    values = inbound_values(mapping, page, instance)
    changed = [
        name for name, value in values.items() if getattr(instance, name) != value
    ]
    advanced = edited is not None and (stored is None or edited > stored)
    return False, changed, values, advanced


def apply_to_record(mapping, pk, page, fetch):
    """Apply the page to one record. Returns (instance, changed_fields, page).

    Returns None if nothing happened because the record is gone or the state
    is stale; `changed_fields` is None when the state brought nothing new.
    The returned page is the one applied, which may be a fresh fetch.
    """
    model = mapping.model
    with transaction.atomic():
        # The lock orders concurrent applies to the same record.
        instance = model._base_manager.select_for_update().filter(pk=pk).first()
        if instance is None or instance.notion_page_id != page.id:
            return None
        stale, changed, values, advanced = compare_state(mapping, instance, page)
        if (
            not stale
            and changed
            and not advanced
            and page.last_edited_time == instance.notion_last_edited_time
        ):
            # Notion keeps edit times to the minute, so an equal time cannot
            # tell this state from the one already applied: it may be older,
            # fetched before a newer state another worker applied first. Read
            # the page again under the lock; every earlier apply has
            # committed, so this read is at least as new as what they wrote.
            fresh = fetch(page.id)
            data_source = get_target(mapping.target).data_source_id
            if (
                fresh is None
                or fresh.in_trash
                or normalize_id(fresh.data_source_id) != normalize_id(data_source)
            ):
                # Gone, trashed or moved since: that change's own event
                # handles it.
                return instance, None, page
            page = fresh
            stale, changed, values, advanced = compare_state(mapping, instance, page)
        if stale:
            logger.debug(
                "Skipped a stale state of page %s for %s %s.",
                page.id,
                model._meta.label,
                pk,
            )
            return None
        if not changed and not advanced:
            return instance, None, page
        for name in changed:
            setattr(instance, name, values[name])
        update_fields = list(changed)
        if advanced:
            instance.notion_last_edited_time = page.last_edited_time
            update_fields.append("notion_last_edited_time")
        if changed:
            # save() so the host's own signals see the change. Naming only
            # In fields and the mixin's field never queues an outbound sync.
            instance.save(update_fields=update_fields)
        else:
            model._base_manager.filter(pk=pk).update(
                notion_last_edited_time=page.last_edited_time
            )
    field_names = [instance._meta.get_field(name).name for name in changed]
    return instance, field_names, page


def make_fetcher(client):
    def fetch(page_id):
        try:
            return pages.retrieve(client, page_id)
        except NotionNotFoundError:
            return None

    return fetch


def apply_page(target, page, event_type, client):
    """Apply a fetched page to every record linked to it and send signals.

    `client` is used to read the page again when its state is ambiguous.
    Returns the number of records whose fields changed.
    """
    target = get_target(target)
    links = linked_records(target, page.id)
    if not links:
        if event_type != RECONCILE:
            send_safely(
                page_changed,
                sender=None,
                target=target.name,
                page=page,
                event_type=event_type,
                instance=None,
                changed_fields=[],
            )
        return 0
    updated = 0
    for mapping, pk in links:
        result = apply_to_record(mapping, pk, page, make_fetcher(client))
        if result is None:
            continue
        instance, changed, applied = result
        if changed is None:
            continue  # Nothing new: an echo, a repeat or an already-seen state.
        updated += bool(changed)
        send_safely(
            page_changed,
            sender=mapping.model,
            target=target.name,
            page=applied,
            event_type=event_type,
            instance=instance,
            changed_fields=changed,
        )
    return updated


def trash_page(target, page, event_type=TRASH_EVENT):
    """Send `page_trashed` and apply each linked record's `on_notion_trash`."""
    target = get_target(target)
    links = linked_records(target, page.id)
    if not links:
        send_safely(
            page_trashed,
            sender=None,
            target=target.name,
            page=page,
            event_type=event_type,
            instance=None,
        )
        return
    for mapping, pk in links:
        model = mapping.model
        instance = model._base_manager.filter(pk=pk, notion_page_id=page.id).first()
        if instance is None:
            continue
        send_safely(
            page_trashed,
            sender=model,
            target=target.name,
            page=page,
            event_type=event_type,
            instance=instance,
        )
        action = mapping.get_on_notion_trash()
        if action == "unlink":
            model._base_manager.filter(pk=pk, notion_page_id=page.id).update(
                notion_page_id=None, notion_last_edited_time=None
            )
            instance.notion_page_id = None
            instance.notion_last_edited_time = None
        elif action == "delete":
            with deleting_for_notion_trash(instance):
                instance.delete()


def process_page_event(target, event_type, page_id, client):
    """Fetch the page an event names and apply it.

    Raises NotionAPIError on a request failure. A page that no longer exists
    or is no longer shared is skipped.
    """
    try:
        page = pages.retrieve(client, page_id)
    except NotionNotFoundError:
        logger.info("Page %s from a %s event is not available.", page_id, event_type)
        return
    if normalize_id(page.data_source_id) != normalize_id(target.data_source_id):
        logger.info(
            "Page %s from a %s event is not in target %s's data source; ignored.",
            page.id,
            event_type,
            target.name,
        )
        return
    if page.in_trash:
        if event_type == TRASH_EVENT:
            trash_page(target, page, event_type)
        # Any other event for a trashed page is out of date; its own
        # page.deleted event handles the trash.
        return
    # Not in the trash, whatever the event says: a page.deleted event for a
    # page restored since is applied like an edit. Notion may merge a quick
    # trash and restore into no event at all, so this is the safe reading.
    apply_page(target, page, event_type, client)


# --- Reconcile -------------------------------------------------------------------


def reconcile(target, *, since=None):
    """Apply every page edited since `since` (None: every page) in the target.

    Returns (pages read, records changed). Pages in the trash are not
    returned by Notion's query, so trashes reach Django only through webhooks.
    """
    target = get_target(target)
    query = {
        "sorts": [{"timestamp": "last_edited_time", "direction": "ascending"}],
        "page_size": QUERY_PAGE_SIZE,
    }
    if since is not None:
        # Notion keeps edit times to the minute.
        since = since.astimezone(UTC).replace(second=0, microsecond=0)
        query["filter"] = {
            "timestamp": "last_edited_time",
            "last_edited_time": {"on_or_after": since.isoformat()},
        }
    read = changed = 0
    with NotionClient(target) as client:
        cursor = None
        while True:
            response = client.query_data_source(**query, start_cursor=cursor)
            for raw in response.get("results", []):
                if raw.get("object") != "page":
                    continue
                read += 1
                changed += apply_page(
                    target, pages.Page.from_notion(raw), RECONCILE, client
                )
            cursor = response.get("next_cursor")
            if not response.get("has_more") or not cursor:
                break
    return read, changed


def default_since():
    """The start of the reconcile window, or None for every page."""
    hours = get_settings().reconcile_lookback_hours
    if not hours:
        return None
    return timezone.now() - timedelta(hours=hours)
