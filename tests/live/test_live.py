"""The live suite. See conftest.py in this folder for what it needs."""

import base64
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from io import StringIO

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command

from notion_sync import api, outbox, pages
from notion_sync.client import NotionClient
from notion_sync.conf import get_target
from notion_sync.exceptions import NotionRequestValidationError
from notion_sync.files import NotionFile
from notion_sync.values import DateRange

# A 1x1 transparent PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA"
    "60e6kgAAAABJRU5ErkJggg=="
)


def wait_until_queryable(target_name, external_id, timeout=30):
    """Notion's query can lag behind a write; wait until it sees the page."""
    target = get_target(target_name)
    deadline = time.monotonic() + timeout
    with NotionClient(target) as client:
        while time.monotonic() < deadline:
            if pages.find_by_external_id(target, client, external_id):
                return
            time.sleep(1)
    pytest.fail(f"Page with external ID {external_id} never showed up in a query")


def test_check_command_passes(live_target):
    out = StringIO()

    call_command(
        "notion_sync_check", "--target", live_target, stdout=out, no_color=True
    )

    assert "All targets are ready." in out.getvalue()


def test_create_update_upsert_and_archive_with_every_property_type(
    live_target, trash_after
):
    external_id = f"live-{uuid.uuid4()}"
    user_id = os.environ.get("NOTION_LIVE_USER_ID")
    owners = [user_id] if user_id else []

    related = api.create_page(live_target, {"Name": "Live suite: related page"})
    trash_after.append(related.id)

    values = {
        "Name": "Live suite: main page",
        "Text": "x" * 2500,
        "Number": 12.5,
        "Select": "High",
        "Tags": ["bug", "web"],
        "Status": "Not started",
        "Due": date(2026, 12, 1),
        "Done": True,
        "Link": "https://example.com/item",
        "Email": "someone@example.com",
        "Phone": "+1 555 0100",
        "Owners": owners,
        "Related": [related.id],
        "Files": NotionFile(ContentFile(PNG, name="pixel.png")),
    }
    page = api.create_page(live_target, values, external_id=external_id)
    trash_after.append(page.id)

    fetched = api.get_page(live_target, page.id)
    files = fetched.properties.pop("Files")
    expected = {**values, "External ID": external_id}
    expected.pop("Files")
    assert {name: fetched.properties[name] for name in expected} == expected
    assert [item["name"] for item in files] == ["pixel.png"]
    assert files[0]["url"].startswith("https://")
    assert fetched.in_trash is False

    updated = api.update_page(
        live_target, page.id, {"Status": "Done", "Tags": [], "Number": None}
    )
    assert updated.properties["Status"] == "Done"
    assert updated.properties["Tags"] == []
    assert updated.properties["Number"] is None
    assert updated.properties["Name"] == "Live suite: main page"

    wait_until_queryable(live_target, external_id)
    upserted = api.upsert_page(live_target, external_id, {"Select": "Low"})
    assert upserted.id == page.id
    assert upserted.properties["Select"] == "Low"

    api.archive_page(live_target, page.id)
    assert api.get_page(live_target, page.id).in_trash is True


@pytest.mark.django_db
def test_the_outbox_sends_a_queued_create(live_target, trash_after):
    item = api.enqueue_create(live_target, {"Name": "Live suite: queued page"})

    counts = outbox.drain()

    item.refresh_from_db()
    if item.result_page_id:
        trash_after.append(item.result_page_id)
    assert counts == {"succeeded": 1}
    page = api.get_page(live_target, item.result_page_id)
    assert page.properties["Name"] == "Live suite: queued page"
    assert page.properties["External ID"] == str(item.id)


def test_datetimes_ranges_external_files_and_clearing(live_target, trash_after):
    user_id = os.environ.get("NOTION_LIVE_USER_ID")
    related = api.create_page(live_target, {"Name": "Live suite: related page"})
    trash_after.append(related.id)
    moment = datetime(2026, 12, 1, 9, 30, tzinfo=timezone(timedelta(hours=2)))

    page = api.create_page(
        live_target,
        {
            "Name": "Live suite: dates and clearing",
            "Text": "Some text",
            "Number": 3,
            "Select": "High",
            "Tags": ["bug"],
            "Due": moment,
            "Done": True,
            "Link": "https://example.com/item",
            "Email": "someone@example.com",
            "Phone": "+1 555 0100",
            "Owners": [user_id] if user_id else [],
            "Related": [related.id],
            "Files": "https://example.com/files/report.pdf",
        },
    )
    trash_after.append(page.id)

    fetched = api.get_page(live_target, page.id).properties
    assert fetched["Due"] == moment
    assert fetched["Files"] == [
        {"name": "report.pdf", "url": "https://example.com/files/report.pdf"}
    ]

    days = DateRange(date(2026, 12, 1), date(2026, 12, 3))
    assert (
        api.update_page(live_target, page.id, {"Due": days}).properties["Due"] == days
    )

    hours = DateRange(moment, moment + timedelta(hours=2))
    assert (
        api.update_page(live_target, page.id, {"Due": hours}).properties["Due"] == hours
    )

    cleared = api.update_page(
        live_target,
        page.id,
        {
            "Text": None,
            "Number": None,
            "Select": None,
            "Tags": [],
            "Due": None,
            "Done": False,
            "Link": None,
            "Email": None,
            "Phone": None,
            "Owners": [],
            "Related": [],
            "Files": [],
        },
    ).properties
    assert {name: cleared[name] for name in ("Text", "Tags", "Owners", "Related")} == {
        "Text": "",
        "Tags": [],
        "Owners": [],
        "Related": [],
    }
    assert cleared["Files"] == []
    assert cleared["Done"] is False
    for name in ("Number", "Select", "Due", "Link", "Email", "Phone"):
        assert cleared[name] is None, name
    assert cleared["Name"] == "Live suite: dates and clearing"


def test_notion_rejects_a_stale_payload_with_a_validation_error(
    live_target, trash_after
):
    """The schema-refresh retry relies on Notion answering 400 validation_error."""
    page = api.create_page(live_target, {"Name": "Live suite: validation"})
    trash_after.append(page.id)
    text = [{"type": "text", "text": {"content": "words"}}]

    with NotionClient(get_target(live_target)) as client:
        with pytest.raises(NotionRequestValidationError):  # A retyped property.
            client.update_page(page.id, properties={"Number": {"rich_text": text}})
        with pytest.raises(NotionRequestValidationError):  # A renamed property.
            client.update_page(
                page.id, properties={"No such property": {"rich_text": text}}
            )


@pytest.mark.django_db
def test_a_mapped_model_syncs_updates_and_archives(
    live_target, live_ticket_mapping, trash_after, settings, storage
):
    Ticket = live_ticket_mapping
    settings.NOTION_SYNC = {
        **settings.NOTION_SYNC,
        "AUTO_SYNC": True,
        "ON_DELETE": "archive",
    }
    out = StringIO()
    call_command(
        "notion_sync_check", "--target", live_target, stdout=out, no_color=True
    )
    assert "testapp.Ticket -> live:" in out.getvalue()

    ticket = Ticket(
        title="Live suite: mapped model",
        body="Body text",
        priority="High",
        tags=["bug"],
        due=date(2026, 12, 1),
        done=False,
        link="",  # Blank in Django clears the URL in Notion.
    )
    ticket.screenshot.save("pixel.png", ContentFile(PNG), save=False)
    ticket.save()

    assert outbox.drain() == {"succeeded": 1}
    ticket.refresh_from_db()
    assert ticket.notion_page_id
    trash_after.append(ticket.notion_page_id)
    page = api.get_page(live_target, ticket.notion_page_id)
    assert page.properties["Name"] == "Live suite: mapped model"
    assert page.properties["Select"] == "High"
    assert page.properties["Link"] is None
    assert page.properties["External ID"] == f"testapp.ticket:{ticket.pk}"
    assert [item["name"] for item in page.properties["Files"]] == ["pixel.png"]

    for title in ("second", "third", "Live suite: mapped model, edited"):
        ticket.title = title
        ticket.save()
    assert outbox.drain() == {"succeeded": 1}
    page = api.get_page(live_target, ticket.notion_page_id)
    assert page.properties["Name"] == "Live suite: mapped model, edited"
    assert len(page.properties["Files"]) == 1  # create_only: not re-uploaded.

    page_id = ticket.notion_page_id
    ticket.delete()
    assert outbox.drain() == {"succeeded": 1}
    assert api.get_page(live_target, page_id).in_trash is True
