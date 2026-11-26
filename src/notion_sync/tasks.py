"""Celery tasks. Install the `celery` extra to use them.

Only this module imports Celery, and the package imports it only when
NOTION_SYNC["DISPATCH"] is "celery".

    CELERY_BEAT_SCHEDULE = {
        "notion-sync-drain": {
            "task": "notion_sync.tasks.drain_outbox",
            "schedule": 60.0,
        },
        "notion-sync-webhooks": {
            "task": "notion_sync.tasks.process_webhook_events",
            "schedule": 60.0,
        },
        "notion-sync-reconcile": {
            "task": "notion_sync.tasks.reconcile",
            "schedule": 3600.0,
        },
    }
"""

from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from notion_sync import inbound, outbox, webhooks
from notion_sync.conf import get_settings

__all__ = [
    "drain_outbox",
    "process_webhook_event",
    "process_webhook_events",
    "reconcile",
    "send_outbox_item",
]


@shared_task(name="notion_sync.tasks.drain_outbox", ignore_result=False)
def drain_outbox(limit: int | None = None) -> dict[str, int]:
    """Send every due outbox row. Returns counts by status."""
    return dict(outbox.drain(limit=limit))


@shared_task(name="notion_sync.tasks.send_outbox_item", ignore_result=False)
def send_outbox_item(item_id: str) -> str | None:
    """Send one outbox row if it is still pending and due. Returns its status.

    Fired when a row commits, with DISPATCH = "celery". A row that fails is
    retried by `drain_outbox` once its backoff has passed, not by Celery.
    """
    item = outbox.drain_one(item_id)
    return item.status if item is not None else None


@shared_task(name="notion_sync.tasks.process_webhook_events", ignore_result=False)
def process_webhook_events(limit: int | None = None) -> dict[str, int]:
    """Process every due stored webhook event. Returns counts by status."""
    return dict(webhooks.process_stored(limit=limit))


@shared_task(name="notion_sync.tasks.process_webhook_event", ignore_result=False)
def process_webhook_event(event_pk: int) -> str | None:
    """Process one stored webhook event if it is pending. Returns its status.

    Fired when an event is stored, with DISPATCH = "celery". An event that
    fails is retried by `process_webhook_events` after its backoff.
    """
    item = webhooks.process_one(event_pk)
    return item.status if item is not None else None


@shared_task(name="notion_sync.tasks.reconcile", ignore_result=False)
def reconcile(
    target: str | None = None, hours: float | None = None
) -> dict[str, list[int]]:
    """Apply pages edited in the lookback window, for one target or all.

    Returns {target: [pages read, records changed]}.
    """
    names = [target] if target else list(get_settings().targets)
    if hours is None:
        since = inbound.default_since()
    else:
        since = timezone.now() - timedelta(hours=hours) if hours else None
    return {name: list(inbound.reconcile(name, since=since)) for name in names}
