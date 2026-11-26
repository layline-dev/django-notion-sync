"""Signals sent by the package.

`page_synced`: an outbound write succeeded.
    Arguments: `target` (name), `operation` ("create", "update", "upsert" or
    "archive"), `page_id`, `page` (a `Page`), `item` (the `OutboxItem`, or
    None for an inline call) and `instance` (the model instance for a model
    sync, else None). For a model sync the sender is the model class.

`sync_failed`: an outbox row was parked and will not be sent again by itself.
    Arguments: `item` (the `OutboxItem`; its `status` is "failed" or
    "needs_review") and `error` (a message).

`page_changed`: a page changed in Notion.
    Arguments: `target` (name), `page` (a `Page`, freshly fetched),
    `event_type` (the webhook event type, such as "page.properties_updated",
    or "reconcile"), `instance` (the linked model instance, or None) and
    `changed_fields` (names of the model fields that were updated; empty if
    only the page's edit time moved). For a linked instance the sender is its
    model class. Sent only when something changed: a webhook event for a page
    no record is linked to, or a linked page whose `In` fields or edit time
    moved on. Reconcile does not send it for pages no record is linked to.

`page_trashed`: a page was moved to the trash in Notion.
    Arguments: `target`, `page`, `event_type` ("page.deleted") and
    `instance` (the linked instance, or None). Sent before the mapping's
    `on_notion_trash` is applied.
"""

import logging
from typing import Any

from django.dispatch import Signal

__all__ = ["page_changed", "page_synced", "page_trashed", "sync_failed"]

logger = logging.getLogger("notion_sync")

page_synced = Signal()
sync_failed = Signal()
page_changed = Signal()
page_trashed = Signal()


def send_safely(signal: Signal, sender: Any, **kwargs: Any) -> None:
    """Send a signal; log receiver errors instead of raising them.

    By the time these signals fire the Notion write has happened or the row
    has been parked, so a failing receiver must not undo or hide that.
    """
    for receiver, response in signal.send_robust(sender=sender, **kwargs):
        if isinstance(response, Exception):
            logger.error(
                "A receiver of a notion_sync signal raised: %r",
                receiver,
                exc_info=response,
            )
