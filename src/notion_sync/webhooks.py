"""Receiving Notion webhook events.

The flow for each POST:

1. A body of only `{"verification_token": ...}` is Notion's one-time
   subscription handshake. The token is held in the cache for 15 minutes so
   `manage.py notion_sync_webhook_token` can show it. It is never logged.
2. Anything else must carry `X-Notion-Signature: sha256=<hex>`, an
   HMAC-SHA256 of the raw body keyed with a target's
   WEBHOOK_VERIFICATION_TOKEN. The targets whose token matches are the
   candidates for the event.
3. Events older than REPLAY_WINDOW (by their signed `timestamp`) and event
   types the package does not act on are acknowledged and dropped.
4. The event is routed to the candidate target whose data source is the
   page's parent (`data.parent.data_source_id`, sent by subscriptions on API
   version 2025-09-03 or later). Other events are acknowledged and dropped.
5. Each event ID is handled once: inline mode remembers IDs in Django's
   cache, stored mode by the unique `WebhookEvent.event_id`.
"""

import hashlib
import hmac
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.utils import timezone

from notion_sync import inbound
from notion_sync.client import NotionClient
from notion_sync.conf import get_settings, get_target
from notion_sync.exceptions import FATAL, NotionAPIError
from notion_sync.models import WebhookEvent
from notion_sync.outbox import MIN_RETRY_SECONDS, backoff_seconds

logger = logging.getLogger("notion_sync")

Status = WebhookEvent.Status

SIGNATURE_HEADER = "X-Notion-Signature"
SIGNATURE_PREFIX = "sha256="
# Notion retries a delivery for about 24 hours. Events older than this are
# dropped, and event IDs are remembered at least this long.
REPLAY_WINDOW = timedelta(hours=48)
HANDSHAKE_SECONDS = 15 * 60
MAX_HANDSHAKE_TOKENS = 10
MAX_TOKEN_LENGTH = 512
TOKENS_KEY = "notion_sync:webhook:verification_tokens"
SEEN_KEY = "notion_sync:webhook:seen:"

# Page events that can change a page's properties or trash state.
HANDLED_EVENTS = frozenset(
    {
        "page.created",
        "page.properties_updated",
        "page.moved",
        "page.undeleted",
        "page.deleted",
    }
)

BATCH_SIZE = 20
ERROR_LIMIT = 2000

# HTTP status codes the view answers with.
OK = 200
BAD_REQUEST = 400
UNAUTHORIZED = 401
SERVER_ERROR = 500
UNAVAILABLE = 503


# --- Signatures --------------------------------------------------------------


def sign(body, token):
    """The X-Notion-Signature value for a body and verification token."""
    digest = hmac.new(token.encode(), body, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


def verified_targets(body, signature):
    """The targets whose verification token produced `signature`."""
    if not signature:
        return []
    given = signature.strip().encode("utf-8", "replace")
    matched = []
    for name in get_settings().targets:
        try:
            target = get_target(name)
        except ImproperlyConfigured:
            continue
        token = target.webhook_verification_token
        if token and hmac.compare_digest(sign(body, token).encode(), given):
            matched.append(target)
    return matched


# --- The subscription handshake ------------------------------------------------


def handshake_token(payload):
    if set(payload) != {"verification_token"}:
        return None
    token = payload["verification_token"]
    if isinstance(token, str) and 0 < len(token) <= MAX_TOKEN_LENGTH:
        return token
    return None


def remember_verification_token(token):
    """Keep a handshake token, with the time it came, for HANDSHAKE_SECONDS.

    At most MAX_HANDSHAKE_TOKENS are kept; the oldest go first.
    """
    kept = [(value, at) for value, at in cache.get(TOKENS_KEY) or [] if value != token]
    kept = [*kept[-(MAX_HANDSHAKE_TOKENS - 1) :], (token, time.time())]
    cache.set(TOKENS_KEY, kept, HANDSHAKE_SECONDS)


def recent_verification_tokens():
    """Tokens received in the last 15 minutes, oldest first, as (token, time)."""
    now = time.time()
    return [
        (value, datetime.fromtimestamp(at, tz=UTC))
        for value, at in cache.get(TOKENS_KEY) or []
        if now - at < HANDSHAKE_SECONDS
    ]


# --- Events -------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    id: str
    type: str
    timestamp: datetime
    entity_id: str
    entity_type: str | None
    data_source_id: str | None


def parse_event(payload):
    """An `Event` from a delivered payload. Raises ValueError if malformed."""
    try:
        event_id = payload["id"]
        event_type = payload["type"]
        entity = payload["entity"]
        entity_id = entity["id"]
        timestamp = datetime.fromisoformat(payload["timestamp"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("not a Notion webhook event") from None
    if not all(isinstance(v, str) and v for v in (event_id, event_type, entity_id)):
        raise ValueError("not a Notion webhook event")
    if len(event_id) > 64 or len(entity_id) > 36:
        raise ValueError("not a Notion webhook event")
    if timezone.is_naive(timestamp):
        timestamp = timestamp.replace(tzinfo=UTC)
    data = payload.get("data")
    parent = data.get("parent") if isinstance(data, dict) else None
    data_source_id = parent.get("data_source_id") if isinstance(parent, dict) else None
    return Event(
        id=event_id,
        type=event_type,
        timestamp=timestamp,
        entity_id=entity_id,
        entity_type=entity.get("type") if isinstance(entity, dict) else None,
        data_source_id=data_source_id if isinstance(data_source_id, str) else None,
    )


def route(event, candidates):
    """The candidate target whose data source holds the event's page, or None."""
    wanted = inbound.normalize_id(event.data_source_id)
    for target in candidates:
        if inbound.normalize_id(target.data_source_id) == wanted:
            return target
    return None


def receive(body, signature):
    """Handle one delivery. Returns the HTTP status code to answer with."""
    try:
        payload = json.loads(body)
    except ValueError:
        return BAD_REQUEST
    if not isinstance(payload, dict):
        return BAD_REQUEST

    token = handshake_token(payload)
    if token is not None:
        remember_verification_token(token)
        logger.info(
            "Received a Notion webhook verification token; show it with "
            "`manage.py notion_sync_webhook_token`."
        )
        return OK

    candidates = verified_targets(body, signature)
    if not candidates:
        logger.warning(
            "Rejected a webhook request: the %s header is missing or matches no "
            "target's WEBHOOK_VERIFICATION_TOKEN.",
            SIGNATURE_HEADER,
        )
        return UNAUTHORIZED

    try:
        event = parse_event(payload)
    except ValueError:
        return BAD_REQUEST

    if timezone.now() - event.timestamp > REPLAY_WINDOW:
        logger.warning(
            "Ignored webhook event %s (%s): it is older than %s.",
            event.id,
            event.type,
            REPLAY_WINDOW,
        )
        return OK
    if event.type not in HANDLED_EVENTS or event.entity_type != "page":
        logger.debug("Ignored webhook event %s (%s).", event.id, event.type)
        return OK
    if not event.data_source_id:
        logger.warning(
            "Ignored webhook event %s (%s): it does not name the page's data "
            "source. Set the subscription's API version to 2025-09-03 or later.",
            event.id,
            event.type,
        )
        return OK
    target = route(event, candidates)
    if target is None:
        logger.debug(
            "Ignored webhook event %s (%s) for a data source no target uses.",
            event.id,
            event.type,
        )
        return OK

    if get_settings().webhook_processing == "stored":
        store(event, target, payload)
        return OK
    return process_inline(event, target)


def process_inline(event, target):
    key = SEEN_KEY + event.id
    if not cache.add(key, True, int(REPLAY_WINDOW.total_seconds())):
        logger.info("Ignored webhook event %s: already received.", event.id)
        return OK
    try:
        with NotionClient(target) as client:
            inbound.process_page_event(target, event.type, event.entity_id, client)
    except NotionAPIError as error:
        if error.outcome == FATAL:
            logger.error(
                "Could not process webhook event %s (%s) for target %s: %s",
                event.id,
                event.type,
                target.name,
                error,
            )
            return OK
        # Answering with an error makes Notion deliver the event again.
        cache.delete(key)
        logger.warning(
            "Webhook event %s (%s) will be retried by Notion: %s",
            event.id,
            event.type,
            error,
        )
        return UNAVAILABLE
    except Exception:
        cache.delete(key)
        logger.exception("Webhook event %s (%s) failed.", event.id, event.type)
        return SERVER_ERROR
    return OK


# --- Stored events -------------------------------------------------------------


def store(event, target, payload):
    """Save a verified event to be processed later. Returns it, or None if seen."""
    try:
        with transaction.atomic():
            item = WebhookEvent.objects.create(
                event_id=event.id,
                type=event.type,
                entity_id=event.entity_id,
                target=target.name,
                payload=payload,
            )
    except IntegrityError:
        logger.info("Ignored webhook event %s: already received.", event.id)
        return None
    dispatch(item)
    return item


def dispatch(item):
    """With DISPATCH = "celery", process the event from a task once committed."""
    if get_settings().dispatch != "celery":
        return
    pk = item.pk

    def send_task():
        try:
            # Celery is optional: notion_sync.tasks imports it at module top.
            from notion_sync.tasks import process_webhook_event  # noqa: PLC0415

            process_webhook_event.delay(pk)
        except Exception:
            logger.warning(
                "Could not queue a Celery task for webhook event %s; the next "
                "drain will process it.",
                item.event_id,
                exc_info=True,
            )

    transaction.on_commit(send_task)


def claim(limit=BATCH_SIZE, *, exclude=(), only=None):
    """Mark up to `limit` due stored events as processing and return them."""
    now = timezone.now()
    with transaction.atomic():
        due = (
            WebhookEvent.objects.filter(status=Status.PENDING, next_attempt_at__lte=now)
            .exclude(pk__in=exclude)
            .order_by("next_attempt_at", "received_at")
        )
        if only is not None:
            due = due.filter(pk__in=only)
        if connection.features.has_select_for_update:
            due = due.select_for_update(
                skip_locked=connection.features.has_select_for_update_skip_locked
            )
        items = list(due[:limit])
        WebhookEvent.objects.filter(pk__in=[item.pk for item in items]).update(
            status=Status.PROCESSING, locked_at=now, attempts=F("attempts") + 1
        )
    for item in items:
        item.status = Status.PROCESSING
        item.locked_at = now
        item.attempts += 1
    return items


def release_stale():
    """Return events left processing by a run that died. Processing is
    idempotent, so they simply go back to pending."""
    cutoff = timezone.now() - timedelta(seconds=get_settings().sending_timeout_seconds)
    return WebhookEvent.objects.filter(
        status=Status.PROCESSING, locked_at__lt=cutoff
    ).update(status=Status.PENDING, locked_at=None, next_attempt_at=timezone.now())


def finish_event(item, status, error="", **changes):
    changes.update(status=status, locked_at=None, error=str(error)[:ERROR_LIMIT])
    for name, value in changes.items():
        setattr(item, name, value)
    item.save(update_fields=list(changes))


def fail_event(item, error):
    finish_event(item, Status.FAILED, error)
    logger.warning(
        "Webhook event %s (%s) failed: %s", item.event_id, item.type, item.error
    )


def process(item):
    """Process one claimed stored event and record the outcome on it."""
    try:
        target = get_target(item.target)
        with NotionClient(target) as client:
            inbound.process_page_event(target, item.type, item.entity_id, client)
    except ImproperlyConfigured as error:
        fail_event(item, error)
    except NotionAPIError as error:
        if error.outcome == FATAL:
            fail_event(item, error)
        elif item.attempts >= get_settings().max_attempts:
            fail_event(item, f"Gave up after {item.attempts} attempts: {error}")
        else:
            delay = error.retry_after
            if delay is None:
                delay = backoff_seconds(item.attempts)
            finish_event(
                item,
                Status.PENDING,
                error,
                next_attempt_at=timezone.now()
                + timedelta(seconds=max(delay, MIN_RETRY_SECONDS)),
            )
    except Exception as error:
        logger.exception("Webhook event %s failed unexpectedly.", item.event_id)
        fail_event(item, f"Unexpected {type(error).__name__}: {error}")
    else:
        finish_event(item, Status.PROCESSED, processed_at=timezone.now())
    return item


def process_one(pk):
    """Process one stored event if it is pending and due. Returns it or None."""
    release_stale()
    for item in claim(1, only=[pk]):
        return process(item)
    return None


def process_stored(limit=None):
    """Process every due stored event, at most `limit`. Returns counts by status."""
    release_stale()
    counts: Counter[str] = Counter()
    seen: list[int] = []
    while limit is None or len(seen) < limit:
        size = BATCH_SIZE if limit is None else min(BATCH_SIZE, limit - len(seen))
        batch = claim(size, exclude=seen)
        if not batch:
            break
        for item in batch:
            seen.append(item.pk)
            process(item)
            counts[item.status] += 1
    return counts


def prune(days=None):
    """Delete processed events older than the retention period.

    They are kept for at least REPLAY_WINDOW, so a repeated delivery is still
    recognised. Failed events stay.
    """
    if days is None:
        days = get_settings().retention_days
    keep = max(timedelta(days=days), REPLAY_WINDOW)
    deleted, _ = WebhookEvent.objects.filter(
        status=Status.PROCESSED, received_at__lt=timezone.now() - keep
    ).delete()
    return deleted
