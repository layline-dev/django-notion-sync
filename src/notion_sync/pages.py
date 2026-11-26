"""Writing pages to Notion, with no knowledge of models or the outbox.

The public functions in `api`, inline model syncs and the outbox all write
through here. Nothing here sends a signal except `send_page_synced`, which
callers invoke once their own bookkeeping is done.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from django.db.models import Model

from notion_sync.client import NotionClient
from notion_sync.conf import Target
from notion_sync.exceptions import NotionRequestValidationError, NotionValidationError
from notion_sync.files import NotionFile, upload_file
from notion_sync.schema import get_schema
from notion_sync.signals import page_synced, send_safely
from notion_sync.values import from_notion, to_notion

if TYPE_CHECKING:
    from notion_sync.models import OutboxItem

__all__ = ["Page"]

logger = logging.getLogger("notion_sync")

# Property types that can hold an external ID and be filtered by equality.
EXTERNAL_ID_TYPES = ("rich_text", "title")


@dataclass(frozen=True)
class Page:
    """A Notion page, with its property values as plain Python."""

    id: str
    url: str | None
    last_edited_time: datetime | None
    properties: dict[str, Any]
    in_trash: bool
    # The data source the page belongs to, or None if its parent is not one.
    data_source_id: str | None = None

    @classmethod
    def from_notion(cls, raw: dict[str, Any]) -> "Page":
        edited = raw.get("last_edited_time")
        return cls(
            id=raw["id"],
            url=raw.get("url"),
            last_edited_time=datetime.fromisoformat(edited) if edited else None,
            properties=from_notion(raw.get("properties") or {}),
            in_trash=bool(raw.get("in_trash", raw.get("archived", False))),
            data_source_id=(raw.get("parent") or {}).get("data_source_id"),
        )


def require_external_id_property(target: Target) -> str:
    if not target.external_id_property:
        raise NotionValidationError(
            f"Target {target.name!r} has no EXTERNAL_ID_PROPERTY, "
            "so it cannot use an external ID."
        )
    return target.external_id_property


def with_external_id(
    target: Target, properties: dict[str, Any], external_id: str | None
) -> dict[str, Any]:
    if external_id is None:
        return properties
    name = require_external_id_property(target)
    if not isinstance(external_id, str) or not external_id:
        raise NotionValidationError("external_id must be a non-empty string.")
    if name in properties and properties[name] != external_id:
        raise NotionValidationError(
            f'Property "{name}" holds the external ID; do not also set it '
            "to a different value.",
            property=name,
            value=properties[name],
        )
    return {**properties, name: external_id}


def send_properties(
    target: Target,
    client: NotionClient,
    properties: dict[str, Any],
    send: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Build the property JSON and send it, with one schema refresh if needed.

    A stale cached schema (a renamed or retyped property) shows up either as
    a local validation error or as Notion's own; both get one retry against a
    freshly fetched schema.
    """
    uploads: dict[int, str] = {}

    def upload(notion_file: NotionFile) -> str:
        if id(notion_file) not in uploads:
            uploads[id(notion_file)] = upload_file(client, notion_file)
        return uploads[id(notion_file)]

    schema = get_schema(target, client)
    try:
        payload = to_notion(schema, properties, upload=upload)
    except NotionValidationError:
        if not schema.from_cache:
            raise
        schema = get_schema(target, client, refresh=True)
        payload = to_notion(schema, properties, upload=upload)

    try:
        return send(payload)
    except NotionRequestValidationError:
        if not schema.from_cache:
            raise
        schema = get_schema(target, client, refresh=True)
        return send(to_notion(schema, properties, upload=upload))


def find_by_external_id(
    target: Target, client: NotionClient, external_id: str
) -> dict[str, Any] | None:
    """The oldest live page whose external ID property equals `external_id`."""
    name = require_external_id_property(target)
    prop = get_schema(target, client).get(name)
    if prop is None or prop.type not in EXTERNAL_ID_TYPES:
        prop = get_schema(target, client, refresh=True).get(name)
    if prop is None:
        raise NotionValidationError(
            f'The data source has no property named "{name}" '
            "(the target's EXTERNAL_ID_PROPERTY).",
            property=name,
        )
    if prop.type not in EXTERNAL_ID_TYPES:
        raise NotionValidationError(
            f'Property "{name}" ({prop.type}) cannot hold the external ID; '
            "use a text property.",
            property=name,
            notion_type=prop.type,
        )
    response = client.query_data_source(
        filter={"property": name, prop.type: {"equals": external_id}},
        sorts=[{"timestamp": "created_time", "direction": "ascending"}],
        page_size=2,
    )
    results = [row for row in response.get("results", []) if row["object"] == "page"]
    if len(results) > 1 or (results and response.get("has_more")):
        logger.warning(
            "Target %s has more than one page with external ID %r; using the oldest.",
            target.name,
            external_id,
        )
    return results[0] if results else None


def create(
    target: Target,
    client: NotionClient,
    properties: dict[str, Any],
    external_id: str | None = None,
) -> Page:
    properties = with_external_id(target, properties, external_id)
    raw = send_properties(target, client, properties, client.create_page)
    return Page.from_notion(raw)


def update(
    target: Target, client: NotionClient, page_id: str, properties: dict[str, Any]
) -> Page:
    raw = send_properties(
        target,
        client,
        properties,
        lambda payload: client.update_page(page_id, properties=payload),
    )
    return Page.from_notion(raw)


def upsert(
    target: Target, client: NotionClient, external_id: str, properties: dict[str, Any]
) -> Page:
    existing = find_by_external_id(target, client, external_id)
    if existing is None:
        return create(target, client, properties, external_id)
    properties = with_external_id(target, properties, external_id)
    return update(target, client, existing["id"], properties)


def archive(target: Target, client: NotionClient, page_id: str) -> Page:
    return Page.from_notion(client.update_page(page_id, in_trash=True))


def retrieve(client: NotionClient, page_id: str) -> Page:
    return Page.from_notion(client.retrieve_page(page_id))


def send_page_synced(
    target: Target,
    operation: str,
    page: Page,
    item: "OutboxItem | None" = None,
    instance: Model | None = None,
) -> None:
    send_safely(
        page_synced,
        sender=type(instance) if instance is not None else None,
        target=target.name,
        operation=operation,
        page_id=page.id,
        page=page,
        item=item,
        instance=instance,
    )
