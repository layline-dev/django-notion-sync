"""Writing a model record's page and linking it back to the record.

Shared by inline syncs (`sync.sync_instance`) and queued ones (the outbox).
"""

from django.utils import timezone

from notion_sync import pages

# Where `sync` keeps the field values seen at post_init, to tell later whether
# a save changed a mapped field. `link` refreshes the link fields in it.
INITIAL_VALUES = "_notion_sync_initial"


def write_page(mapping, target, client, instance):
    """Create or update the instance's page. Returns (page, operation)."""
    page_id = instance.notion_page_id
    properties = mapping.get_properties(instance, creating=not page_id)
    if page_id:
        return pages.update(target, client, page_id, properties), "update"
    if target.external_id_property:
        # A page from an earlier, unlinked attempt is found and updated.
        external_id = mapping.get_external_id(instance)
        existing = pages.find_by_external_id(target, client, external_id)
        if existing is not None:
            return pages.update(target, client, existing["id"], properties), "update"
        return pages.create(target, client, properties, external_id), "create"
    return pages.create(target, client, properties), "create"


def link(instance, page):
    """Store the page on the instance without calling save() or its signals."""
    values = {
        "notion_page_id": page.id,
        "notion_synced_at": timezone.now(),
        "notion_last_edited_time": page.last_edited_time,
    }
    type(instance)._base_manager.filter(pk=instance.pk).update(**values)
    for name, value in values.items():
        setattr(instance, name, value)
    initial = instance.__dict__.get(INITIAL_VALUES)
    if initial is not None:
        initial.update(values)
