"""Inbound sync against the real Notion API.

A real webhook needs a public URL and a subscription made in Notion, so these
tests feed signed events straight to `webhooks.receive`. Everything after
the delivery runs for real: the page fetch, routing by the fetched parent,
trash state and applying values.
"""

import json
import time
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from notion_sync import api, inbound, webhooks
from notion_sync.models import OutboxItem

pytestmark = pytest.mark.django_db

VERIFICATION_TOKEN = "secret_live-suite-only"


@pytest.fixture
def live_inbound(live_target, live_ticket_mapping, settings):
    target = settings.NOTION_SYNC["TARGETS"][live_target]
    settings.NOTION_SYNC = {
        **settings.NOTION_SYNC,
        "TARGETS": {
            live_target: {**target, "WEBHOOK_VERIFICATION_TOKEN": VERIFICATION_TOKEN}
        },
        "AUTO_SYNC": True,
    }
    return live_ticket_mapping, target["DATA_SOURCE_ID"]


def new_linked_ticket(Ticket, trash_after):
    ticket = Ticket.objects.create(title="Live suite: inbound", status="")
    page = ticket.sync_to_notion()
    trash_after.append(page.id)
    OutboxItem.objects.all().delete()
    ticket.refresh_from_db()
    return ticket, page


def deliver(event_type, page_id, data_source_id):
    body = json.dumps(
        {
            "id": str(uuid.uuid4()),
            "timestamp": timezone.now().isoformat(),
            "type": event_type,
            "entity": {"id": page_id, "type": "page"},
            "data": {
                "parent": {
                    "id": str(uuid.uuid4()),
                    "type": "database",
                    "data_source_id": data_source_id,
                }
            },
        }
    ).encode()
    return webhooks.receive(body, webhooks.sign(body, VERIFICATION_TOKEN))


def test_a_signed_event_applies_a_real_status_change(live_inbound, trash_after):
    Ticket, data_source_id = live_inbound
    ticket, page = new_linked_ticket(Ticket, trash_after)
    # Notion gives a new page the status property's default option.
    assert page.properties["Status"] == "Not started"
    assert page.data_source_id.replace("-", "") == data_source_id.replace("-", "")

    # A change made in Notion (by the integration, standing in for a person).
    api.update_page("live", page.id, {"Status": "Done"})

    assert deliver("page.properties_updated", page.id, data_source_id) == 200
    ticket.refresh_from_db()
    assert ticket.status == "Done"
    # Applying it queued no write back to Notion.
    assert OutboxItem.objects.count() == 0


def test_a_signed_trash_event_unlinks_the_record(live_inbound, trash_after, settings):
    Ticket, data_source_id = live_inbound
    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "ON_NOTION_TRASH": "unlink"}
    ticket, page = new_linked_ticket(Ticket, trash_after)

    api.archive_page("live", page.id)
    assert deliver("page.deleted", page.id, data_source_id) == 200

    ticket.refresh_from_db()
    assert ticket.notion_page_id is None
    assert OutboxItem.objects.count() == 0


def test_reconcile_picks_up_a_real_edit(live_inbound, trash_after):
    Ticket, _ = live_inbound
    ticket, page = new_linked_ticket(Ticket, trash_after)
    api.update_page("live", page.id, {"Status": "Done"})
    since = timezone.now() - timedelta(minutes=10)

    # Notion's query can lag a few seconds behind a write.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        inbound.reconcile("live", since=since)
        ticket.refresh_from_db()
        if ticket.status == "Done":
            break
        time.sleep(2)

    assert ticket.status == "Done"
    assert OutboxItem.objects.count() == 0


def test_edit_times_come_in_whole_minutes(live_target, trash_after):
    """The stale-state guard applies equal times because of this."""
    page = api.create_page(live_target, {"Name": "Live suite: edit time"})
    trash_after.append(page.id)
    page = api.get_page(live_target, page.id)

    assert page.last_edited_time.second == 0
    assert page.last_edited_time.microsecond == 0
