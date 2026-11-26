"""Fixtures for the live suite, which calls the real Notion API.

Every test under tests/live/ is marked `live` and is skipped unless
NOTION_LIVE_TOKEN is set (see tests/conftest.py). The suite also needs
NOTION_LIVE_DATA_SOURCE_ID: the data source of a throwaway database, shared
with the integration that owns the token, with these properties:

    Name          title
    Text          text
    Number        number
    Select        select
    Tags          multi-select
    Status        status, with the default "Not started" and "Done" options
    Due           date
    Done          checkbox
    Link          URL
    Email         email
    Phone         phone
    Owners        person
    Related       relation to this same database
    Files         files & media
    External ID   text

Set NOTION_LIVE_USER_ID to a workspace member's user ID to also write the
Owners property; without it that property is written empty.

The tests move every page they create to the trash.
"""

import os

import pytest

import notion_sync
from notion_sync import api
from tests.testapp.models import Ticket
from tests.testapp.notion_mappings import TicketMapping


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/live/" in item.nodeid:
            item.add_marker(pytest.mark.live)


@pytest.fixture
def live_target(settings):
    data_source_id = os.environ.get("NOTION_LIVE_DATA_SOURCE_ID")
    if not data_source_id:
        pytest.fail(
            "NOTION_LIVE_TOKEN is set but NOTION_LIVE_DATA_SOURCE_ID is not",
            pytrace=False,
        )
    settings.NOTION_SYNC = {
        "TARGETS": {
            "live": {
                "TOKEN": os.environ["NOTION_LIVE_TOKEN"],
                "DATA_SOURCE_ID": data_source_id,
                "EXTERNAL_ID_PROPERTY": "External ID",
            },
        },
    }
    return "live"


@pytest.fixture
def trash_after(live_target):
    """Collect page IDs; trash them all when the test ends."""
    page_ids = []
    yield page_ids
    for page_id in page_ids:
        try:
            api.archive_page(live_target, page_id)
        except Exception:  # noqa: BLE001, S110 - best-effort cleanup
            pass


@pytest.fixture
def live_ticket_mapping(live_target):
    """Point the test app's Ticket mapping at the live target for one test."""
    live = type("LiveTicketMapping", (TicketMapping,), {"target": live_target})
    notion_sync.unregister(Ticket)
    notion_sync.register(Ticket)(live)
    yield Ticket
    notion_sync.unregister(Ticket)
    notion_sync.register(Ticket)(TicketMapping)
