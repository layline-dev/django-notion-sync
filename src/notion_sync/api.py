"""The public functions.

The inline functions call Notion now and raise on failure. The `enqueue_*`
functions store an outbox row and return at once; `notion_sync_drain` sends it.
"""

from typing import Any

from notion_sync import outbox, pages
from notion_sync.client import NotionClient
from notion_sync.conf import get_target
from notion_sync.exceptions import NotionValidationError
from notion_sync.models import OutboxItem
from notion_sync.pages import Page

__all__ = [
    "archive_page",
    "create_page",
    "enqueue_archive",
    "enqueue_create",
    "enqueue_update",
    "enqueue_upsert",
    "get_page",
    "update_page",
    "upsert_page",
]


def _check_properties(properties: object) -> None:
    if not isinstance(properties, dict):
        raise NotionValidationError("Properties must be a dict of name to value.")


def create_page(
    target: str, properties: dict[str, Any], *, external_id: str | None = None
) -> Page:
    """Create a page in the target and return it."""
    _check_properties(properties)
    config = get_target(target)
    with NotionClient(config) as client:
        page = pages.create(config, client, properties, external_id)
    pages.send_page_synced(config, "create", page)
    return page


def update_page(target: str, page_id: str, properties: dict[str, Any]) -> Page:
    """Set the given properties on a page and return the page."""
    _check_properties(properties)
    config = get_target(target)
    with NotionClient(config) as client:
        page = pages.update(config, client, page_id, properties)
    pages.send_page_synced(config, "update", page)
    return page


def upsert_page(target: str, external_id: str, properties: dict[str, Any]) -> Page:
    """Update the page with this external ID, or create it if there is none.

    Notion's query can lag a few seconds behind a write, so two upserts of a
    new external ID in quick succession can create two pages.
    """
    _check_properties(properties)
    config = get_target(target)
    with NotionClient(config) as client:
        page = pages.upsert(config, client, external_id, properties)
    pages.send_page_synced(config, "upsert", page)
    return page


def archive_page(target: str, page_id: str) -> None:
    """Move a page to the trash in Notion."""
    config = get_target(target)
    with NotionClient(config) as client:
        page = pages.archive(config, client, page_id)
    pages.send_page_synced(config, "archive", page)


def get_page(target: str, page_id: str) -> Page:
    """Fetch a page, with its property values as plain Python."""
    config = get_target(target)
    with NotionClient(config) as client:
        return pages.retrieve(client, page_id)


def enqueue_create(
    target: str, properties: dict[str, Any], *, external_id: str | None = None
) -> OutboxItem:
    """Queue a page creation. Returns the `OutboxItem`."""
    return outbox.enqueue("create", target, properties, external_id=external_id)


def enqueue_update(target: str, page_id: str, properties: dict[str, Any]) -> OutboxItem:
    """Queue a property update. Returns the `OutboxItem`."""
    return outbox.enqueue("update", target, properties, page_id=page_id)


def enqueue_upsert(
    target: str, external_id: str, properties: dict[str, Any]
) -> OutboxItem:
    """Queue an upsert by external ID. Returns the `OutboxItem`."""
    return outbox.enqueue("upsert", target, properties, external_id=external_id)


def enqueue_archive(target: str, page_id: str) -> OutboxItem:
    """Queue moving a page to the trash. Returns the `OutboxItem`."""
    return outbox.enqueue("archive", target, {}, page_id=page_id)
