import copy
import json
import logging
import threading
from datetime import UTC, date, datetime, timedelta
from io import StringIO

import pytest
from django.conf import settings as django_settings
from django.core.management import CommandError, call_command
from django.db import connection, models
from django.utils import timezone

from notion_sync import In, Out, inbound
from notion_sync.client import NotionClient
from notion_sync.conf import get_target
from notion_sync.models import OutboxItem
from notion_sync.pages import Page
from notion_sync.signals import page_changed
from notion_sync.tasks import reconcile
from notion_sync.values import DateRange
from tests.fake_notion import error_response
from tests.testapp.models import Ticket, Unmapped

pytestmark = pytest.mark.django_db


def status(name):
    return {"status": {"name": name}}


def linked_ticket(notion, value="Not started", edited="2026-11-22T10:00:00.000Z"):
    page = notion.add_page({"Status": status(value)})
    notion.pages[page["id"]]["last_edited_time"] = edited
    ticket = Ticket.objects.create(title="A ticket", status=value)
    Ticket.objects.filter(pk=ticket.pk).update(
        notion_page_id=page["id"], notion_last_edited_time=edited
    )
    ticket.refresh_from_db()
    return ticket, page


@pytest.fixture
def changes():
    received = []

    def on_changed(sender, **kwargs):
        received.append(kwargs)

    page_changed.connect(on_changed)
    yield received
    page_changed.disconnect(on_changed)


def queries(notion):
    return [json.loads(r.content) for r in notion.find("POST", "/query")]


# --- Reconcile ---------------------------------------------------------------


def test_reconcile_applies_pages_edited_in_the_window(notion, changes):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited="2026-11-22T11:00:00Z")
    since = datetime(2026, 11, 22, 10, 30, 45, tzinfo=UTC)

    assert inbound.reconcile("inbox", since=since) == (1, 1)

    ticket.refresh_from_db()
    assert ticket.status == "Done"
    [query] = queries(notion)
    # Notion keeps edit times to the minute, so the window starts on one.
    assert query["filter"] == {
        "timestamp": "last_edited_time",
        "last_edited_time": {"on_or_after": "2026-11-22T10:30:00+00:00"},
    }
    assert query["sorts"] == [
        {"timestamp": "last_edited_time", "direction": "ascending"}
    ]
    assert changes[0]["event_type"] == "reconcile"
    assert changes[0]["changed_fields"] == ["status"]


def test_reconcile_skips_pages_edited_before_the_window(notion):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited="2026-11-22T09:00:00Z")

    assert inbound.reconcile("inbox", since=datetime(2026, 11, 22, 10, tzinfo=UTC)) == (
        0,
        0,
    )
    ticket.refresh_from_db()
    assert ticket.status == "Not started"


def test_reconcile_is_quiet_about_pages_it_has_already_applied(notion, changes):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited="2026-11-22T11:00:00Z")
    notion.add_page({"Status": status("Done")})  # Linked to no record.

    inbound.reconcile("inbox")
    inbound.reconcile("inbox")

    assert len(changes) == 1
    assert changes[0]["instance"].pk == ticket.pk


def test_reconcile_reads_every_page_of_results(notion, monkeypatch):
    monkeypatch.setattr(inbound, "QUERY_PAGE_SIZE", 2)
    tickets = []
    for _ in range(5):
        ticket, page = linked_ticket(notion)
        notion.edit(page["id"], {"Status": status("Done")}, edited="2026-11-22T11:00Z")
        tickets.append(ticket)

    assert inbound.reconcile("inbox") == (5, 5)

    assert len(queries(notion)) == 3
    assert set(Ticket.objects.values_list("status", flat=True)) == {"Done"}


def test_reconcile_queues_no_outbound_sync(notion, settings):
    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "AUTO_SYNC": True}
    ticket, page = linked_ticket(notion)
    OutboxItem.objects.all().delete()
    notion.edit(page["id"], {"Status": status("Done")}, edited="2026-11-22T11:00:00Z")

    inbound.reconcile("inbox")

    assert OutboxItem.objects.count() == 0


def test_reconcile_skips_stale_states(notion):
    ticket, page = linked_ticket(notion, edited="2026-11-22T12:00:00Z")
    notion.edit(page["id"], {"Status": status("Done")}, edited="2026-11-22T11:00:00Z")

    assert inbound.reconcile("inbox") == (1, 0)
    ticket.refresh_from_db()
    assert ticket.status == "Not started"


def test_the_default_window_comes_from_the_setting(settings):
    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "RECONCILE_LOOKBACK_HOURS": 6}
    since = inbound.default_since()
    assert (
        timedelta(hours=5, minutes=59)
        < timezone.now() - since
        < timedelta(hours=6, minutes=1)
    )

    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "RECONCILE_LOOKBACK_HOURS": 0}
    assert inbound.default_since() is None


def run(*args):
    out = StringIO()
    call_command("notion_sync_reconcile", *args, stdout=out)
    return out.getvalue()


def test_the_reconcile_command(notion):
    ticket, page = linked_ticket(notion)
    notion.edit(
        page["id"], {"Status": status("Done")}, edited=timezone.now().isoformat()
    )

    assert run("--target", "inbox") == "inbox: read 1 pages, updated 1 records.\n"
    ticket.refresh_from_db()
    assert ticket.status == "Done"


def test_the_reconcile_command_reads_every_target_by_default(notion):
    assert run() == (
        "inbox: read 0 pages, updated 0 records.\n"
        "plain: read 0 pages, updated 0 records.\n"
    )


def test_the_reconcile_command_window_options(notion):
    run("--target", "inbox", "--since", "2026-11-20")
    run("--target", "inbox", "--since", "2026-11-20T08:15:00+01:00")
    run("--target", "inbox", "--all")
    filters = [query.get("filter") for query in queries(notion)]

    assert filters[0]["last_edited_time"]["on_or_after"] == "2026-11-20T00:00:00+00:00"
    assert filters[1]["last_edited_time"]["on_or_after"] == "2026-11-20T07:15:00+00:00"
    assert filters[2] is None


def test_the_reconcile_command_rejects_a_bad_since(notion):
    with pytest.raises(CommandError, match="ISO 8601"):
        run("--since", "yesterday")


def test_the_reconcile_command_reports_a_failed_target(notion):
    notion.fail_next("POST", "/data_sources/ds-inbox/query", error_response(502, "x"))

    err = StringIO()
    with pytest.raises(CommandError, match="1 target"):
        call_command("notion_sync_reconcile", stdout=StringIO(), stderr=err)
    assert "inbox" in err.getvalue()


def test_the_reconcile_task(notion):
    ticket, page = linked_ticket(notion)
    notion.edit(
        page["id"], {"Status": status("Done")}, edited=timezone.now().isoformat()
    )

    assert reconcile.apply(kwargs={"target": "inbox", "hours": 1}).get() == {
        "inbox": [1, 1]
    }
    assert reconcile.apply().get() == {"inbox": [1, 0], "plain": [0, 0]}
    assert len(queries(notion)) == 3


# --- Values -----------------------------------------------------------------


def field(name):
    return Ticket._meta.get_field(name)


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("status", None, ""),
        ("status", "Done", "Done"),
        ("title", 42, "42"),
        ("due", None, None),
        ("due", date(2026, 12, 1), date(2026, 12, 1)),
        ("due", DateRange(date(2026, 12, 1), date(2026, 12, 3)), date(2026, 12, 1)),
        ("due", datetime(2026, 12, 1, 23, 30, tzinfo=UTC), date(2026, 12, 1)),
        ("done", None, False),
        ("done", True, True),
        ("tags", ["a", "b"], ["a", "b"]),
    ],
)
def test_values_are_converted_for_the_field(name, value, expected):
    assert inbound.field_value(field(name), value, Ticket()) == expected


def test_a_date_becomes_an_aware_datetime():
    created = models.DateTimeField(null=True)
    created.set_attributes_from_name("created")

    assert inbound.field_value(created, date(2026, 12, 1), Ticket()) == datetime(
        2026, 12, 1, tzinfo=UTC
    )
    naive = inbound.field_value(created, datetime(2026, 12, 1, 9), Ticket())
    assert timezone.is_aware(naive)


@pytest.mark.parametrize(
    ("name", "value"),
    [("status", "x" * 21), ("title", None), ("link", "not a url")],
)
def test_values_that_do_not_fit_are_refused(name, value):
    with pytest.raises(inbound.UnusableValue):
        inbound.field_value(field(name), value, Ticket())


def test_a_value_that_does_not_fit_is_left_unchanged_and_logged(notion, caplog):
    caplog.set_level(logging.WARNING)
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("x" * 30)}, edited="2026-11-22T11:00Z")

    inbound.reconcile("inbox")

    ticket.refresh_from_db()
    assert ticket.status == "Not started"
    assert ticket.notion_last_edited_time.isoformat() == "2026-11-22T11:00:00+00:00"
    assert 'Property "Status"' in caplog.text


def test_a_missing_property_is_left_unchanged_and_logged(notion, caplog):
    ticket, page = linked_ticket(notion)
    del notion.pages[page["id"]]["properties"]["Status"]
    notion.edit(page["id"], edited="2026-11-22T11:00Z")

    inbound.reconcile("inbox")

    ticket.refresh_from_db()
    assert ticket.status == "Not started"
    assert 'no property "Status"' in caplog.text


def test_ids_compare_with_or_without_dashes():
    assert inbound.normalize_id("2F26EE68-df30") == inbound.normalize_id("2f26ee68df30")
    assert inbound.normalize_id(None) == ""


def test_applying_saves_only_inbound_fields_so_no_sync_is_queued(notion, register):
    """With a callable Out source any full save() counts as a change, so this
    fails unless inbound apply saves with update_fields."""
    register(
        Unmapped,
        fields={"Name": Out(lambda obj: obj.name.upper()), "Status": In("name")},
        auto_sync=True,
    )
    page = notion.add_page({"Status": status("Not started")})
    record = Unmapped.objects.create(name="Not started")
    OutboxItem.objects.all().delete()
    Unmapped.objects.filter(pk=record.pk).update(
        notion_page_id=page["id"], notion_last_edited_time=page["last_edited_time"]
    )
    notion.edit(page["id"], {"Status": status("Done")}, edited="2026-11-22T11:00Z")

    inbound.reconcile("inbox")

    record.refresh_from_db()
    assert record.name == "Done"
    assert OutboxItem.objects.count() == 0


# --- Concurrent applies with equal edit times ---

SAME_MINUTE = "2026-11-22T10:05:00.000Z"


def snapshot(notion, page_id, value):
    """A Page as a worker would have fetched it, with Status = value."""
    raw = copy.deepcopy(notion.pages[page_id])
    raw["properties"]["Status"] = {"id": "prop", "type": "status", **status(value)}
    return Page.from_notion(raw)


def test_an_older_state_with_the_same_edit_time_does_not_win(notion, changes):
    """The losing order of a race: a newer state was applied first, then an
    older snapshot with the same (minute) edit time arrives."""
    ticket, page = linked_ticket(notion, edited=SAME_MINUTE)
    notion.edit(page["id"], {"Status": status("Done")}, edited=SAME_MINUTE)
    Ticket.objects.filter(pk=ticket.pk).update(status="Done")  # Applied first.
    older = snapshot(notion, page["id"], "In progress")

    with NotionClient(get_target("inbox")) as client:
        inbound.apply_page("inbox", older, "page.properties_updated", client)

    ticket.refresh_from_db()
    assert ticket.status == "Done"
    assert changes == []
    assert notion.count("GET", "/v1/pages/") == 1  # Read again under the lock.


def test_an_equal_time_state_that_changes_nothing_is_not_fetched_again(notion):
    ticket, page = linked_ticket(notion, value="Done", edited=SAME_MINUTE)
    same = snapshot(notion, page["id"], "Done")

    with NotionClient(get_target("inbox")) as client:
        inbound.apply_page("inbox", same, "page.properties_updated", client)

    assert notion.count("GET", "/v1/pages/") == 0


@pytest.mark.parametrize("change", ["trashed", "gone", "moved"])
def test_a_page_that_changed_place_since_is_left_to_its_own_event(notion, change):
    ticket, page = linked_ticket(notion, edited=SAME_MINUTE)
    older = snapshot(notion, page["id"], "Done")
    if change == "trashed":
        notion.edit(page["id"], in_trash=True, edited=SAME_MINUTE)
    elif change == "gone":
        del notion.pages[page["id"]]
    else:
        notion.pages[page["id"]]["parent"]["data_source_id"] = "ds-elsewhere"

    with NotionClient(get_target("inbox")) as client:
        inbound.apply_page("inbox", older, "page.properties_updated", client)

    ticket.refresh_from_db()
    assert ticket.status == "Not started"


@pytest.mark.skipif(
    django_settings.TEST_DB != "postgres", reason="needs row locks (Postgres)"
)
@pytest.mark.django_db(transaction=True)
def test_two_workers_racing_on_one_page_end_on_the_newest_state(notion, monkeypatch):
    """Worker A fetches the page, then stalls; worker B fetches a newer state
    from the same minute and applies it; A carries on with its older copy."""
    ticket, page = linked_ticket(notion, edited=SAME_MINUTE)
    notion.edit(page["id"], {"Status": status("In progress")}, edited=SAME_MINUTE)
    target = get_target("inbox")
    a_fetched, b_done = threading.Event(), threading.Event()
    real_retrieve = NotionClient.retrieve_page
    calls = {"a": 0}

    def retrieve(self, page_id):
        result = real_retrieve(self, page_id)
        if threading.current_thread().name == "worker-a":
            calls["a"] += 1
            if calls["a"] == 1:
                a_fetched.set()
                assert b_done.wait(10)
        return result

    monkeypatch.setattr(NotionClient, "retrieve_page", retrieve)
    errors = []

    def worker_a():
        try:
            with NotionClient(target) as client:
                inbound.process_page_event(
                    target, "page.properties_updated", page["id"], client
                )
        except Exception as error:  # noqa: BLE001 - reported below
            errors.append(error)
        finally:
            connection.close()

    thread = threading.Thread(target=worker_a, name="worker-a")
    thread.start()
    assert a_fetched.wait(10)
    # A person edits again in the same minute; worker B sees and applies it.
    notion.edit(page["id"], {"Status": status("Done")}, edited=SAME_MINUTE)
    with NotionClient(target) as client:
        inbound.process_page_event(
            target, "page.properties_updated", page["id"], client
        )
    b_done.set()
    thread.join(10)

    assert not errors
    ticket.refresh_from_db()
    assert ticket.status == "Done"


def test_page_changed_carries_the_state_read_under_the_lock(notion, changes):
    ticket, page = linked_ticket(notion, value="In progress", edited=SAME_MINUTE)
    older = snapshot(notion, page["id"], "Blocked")
    notion.edit(page["id"], {"Status": status("Done")}, edited=SAME_MINUTE)

    with NotionClient(get_target("inbox")) as client:
        inbound.apply_page("inbox", older, "page.properties_updated", client)

    ticket.refresh_from_db()
    assert ticket.status == "Done"
    [change] = changes
    assert change["page"].properties["Status"] == "Done"
    assert change["changed_fields"] == ["status"]
