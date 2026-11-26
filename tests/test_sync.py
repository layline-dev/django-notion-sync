import threading
from datetime import date, timedelta

import httpx
import pytest
from django.conf import settings as django_settings
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.db import connection, transaction
from django.utils import timezone

from notion_sync import outbox
from notion_sync.exceptions import NotionAPIError, NotionValidationError
from notion_sync.models import OutboxItem
from tests.fake_notion import error_response
from tests.testapp.models import Note, Ticket

pytestmark = pytest.mark.django_db

Status = OutboxItem.Status


def text(content):
    return {"rich_text": [{"type": "text", "text": {"content": content}}]}


def plain(page, name):
    return "".join(item["plain_text"] for item in page["properties"][name]["title"])


def rows(instance=None, **filters):
    queryset = OutboxItem.objects.all()
    if instance is not None:
        queryset = queryset.filter(
            content_type=ContentType.objects.get_for_model(instance),
            object_id=str(instance.pk),
        )
    return list(queryset.filter(**filters).order_by("created_at"))


def make_due(item):
    OutboxItem.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now())


@pytest.fixture
def auto_sync(settings):
    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "AUTO_SYNC": True}


@pytest.fixture
def archive_on_delete(settings):
    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "ON_DELETE": "archive"}


# --- Inline sync --------------------------------------------------------------


def test_sync_to_notion_creates_the_page_and_stores_its_id(notion, captured_signals):
    ticket = Ticket.objects.create(
        title="Checkout broken", priority="High", tags=["bug"], due=date(2026, 12, 1)
    )

    page = ticket.sync_to_notion()

    assert notion.count("POST", "/pages") == 1
    stored = notion.pages[page.id]
    assert plain(stored, "Name") == "Checkout broken"
    assert stored["properties"]["External ID"]["rich_text"][0]["plain_text"] == (
        f"testapp.ticket:{ticket.pk}"
    )
    assert "Status" not in stored["properties"]  # Inbound only.
    ticket.refresh_from_db()
    assert ticket.notion_page_id == page.id
    assert ticket.notion_synced_at is not None
    assert ticket.notion_last_edited_time == page.last_edited_time
    [(name, kwargs)] = captured_signals
    assert name == "page_synced"
    assert kwargs["operation"] == "create"
    assert kwargs["instance"] is ticket
    assert kwargs["item"] is None


def test_a_second_sync_updates_the_same_page(notion, storage, captured_signals):
    ticket = Ticket.objects.create(title="A")
    ticket.screenshot.save("shot.png", ContentFile(b"png"))
    first = ticket.sync_to_notion()
    assert notion.last_json("POST", "/pages")["properties"]["Files"]["files"]

    ticket.title = "B"
    ticket.save()
    second = ticket.sync_to_notion()

    assert second.id == first.id
    assert notion.count("POST", "/pages") == 1
    body = notion.last_json("PATCH", f"/pages/{first.id}")
    assert body["properties"]["Name"]["title"][0]["text"]["content"] == "B"
    assert "Files" not in body["properties"]  # create_only.
    assert len(notion.uploads) == 1
    assert captured_signals[-1][1]["operation"] == "update"


def test_an_unlinked_page_is_found_by_external_id_and_linked(notion):
    ticket = Ticket.objects.create(title="A")
    existing = notion.add_page(
        {"Name": {"title": []}, "External ID": text(f"testapp.ticket:{ticket.pk}")}
    )

    page = ticket.sync_to_notion()

    assert page.id == existing["id"]
    assert notion.count("POST", "/pages") == 0
    ticket.refresh_from_db()
    assert ticket.notion_page_id == existing["id"]


def test_a_target_without_external_ids_creates_without_a_query(notion):
    note = Note.objects.create(text="two words")

    page = note.sync_to_notion()

    assert notion.count("POST", "/query") == 0
    assert notion.last_json("POST", "/pages")["properties"]["Number"] == {"number": 2}
    note.refresh_from_db()
    assert note.notion_page_id == page.id


def test_sync_needs_a_saved_record(no_network):
    with pytest.raises(NotionValidationError, match="Save the record"):
        Ticket(title="A").sync_to_notion()
    with pytest.raises(NotionValidationError, match="Save the record"):
        Ticket(title="A").enqueue_notion_sync()


def test_a_failed_inline_sync_raises_and_links_nothing(notion):
    ticket = Ticket.objects.create(title="A")
    notion.fail_next("POST", "/pages", error_response(503, "service_unavailable"))

    with pytest.raises(NotionAPIError):
        ticket.sync_to_notion()

    ticket.refresh_from_db()
    assert ticket.notion_page_id is None
    assert rows() == []  # Inline calls are not queued or retried.


def test_linking_does_not_save_the_record_or_queue_a_sync(notion, auto_sync):
    ticket = Ticket.objects.create(title="A", notes="draft")
    [item] = rows(ticket)
    OutboxItem.objects.filter(pk=item.pk).delete()
    Ticket.objects.filter(pk=ticket.pk).update(notes="edited elsewhere")

    ticket.sync_to_notion()

    ticket.refresh_from_db()
    assert ticket.notes == "edited elsewhere"
    assert rows() == []


# --- Queued sync --------------------------------------------------------------


def test_enqueue_stores_a_reference_not_values(no_network):
    ticket = Ticket.objects.create(title="A")

    item = ticket.enqueue_notion_sync()

    item.refresh_from_db()
    assert item.operation == "sync"
    assert item.target == "inbox"
    assert item.payload == {}
    assert item.content_type == ContentType.objects.get_for_model(Ticket)
    assert item.object_id == str(ticket.pk)
    assert ticket.enqueue_notion_sync().pk == item.pk
    assert len(rows()) == 1


def test_a_queued_sync_reads_the_values_when_it_is_sent(
    drain, notion, captured_signals
):
    ticket = Ticket.objects.create(title="Old")
    item = ticket.enqueue_notion_sync()
    Ticket.objects.filter(pk=ticket.pk).update(title="New")

    assert drain() == {"succeeded": 1}

    [page] = notion.pages.values()
    assert plain(page, "Name") == "New"
    ticket.refresh_from_db()
    item.refresh_from_db()
    assert ticket.notion_page_id == page["id"]
    assert item.result_page_id == page["id"]
    [(_, kwargs)] = captured_signals
    assert kwargs["operation"] == "create"
    assert kwargs["item"].pk == item.pk
    assert kwargs["instance"].pk == ticket.pk


def test_five_quick_saves_make_one_write_with_the_last_values(drain, notion, auto_sync):
    ticket = Ticket.objects.create(title="v1")
    for version in range(2, 6):
        ticket.title = f"v{version}"
        ticket.save()

    assert len(rows(ticket)) == 1
    assert drain() == {"succeeded": 1}
    assert notion.count("POST", "/pages") + notion.count("PATCH", "/pages/") == 1
    [page] = notion.pages.values()
    assert plain(page, "Name") == "v5"


def test_the_next_save_after_a_send_updates_the_page(drain, notion, auto_sync):
    ticket = Ticket.objects.create(title="v1")
    drain()
    ticket.refresh_from_db()

    ticket.title = "v2"
    ticket.save()

    assert drain() == {"succeeded": 1}
    assert notion.count("POST", "/pages") == 1
    body = notion.last_json("PATCH", f"/pages/{ticket.notion_page_id}")
    assert body["properties"]["Name"]["title"][0]["text"]["content"] == "v2"


def test_a_record_deleted_before_sending_is_skipped(drain, notion):
    ticket = Ticket.objects.create(title="A")
    item = ticket.enqueue_notion_sync()
    ticket.delete()

    assert drain() == {"skipped": 1}

    item.refresh_from_db()
    assert "deleted before it was sent" in item.last_error
    assert notion.requests == []


def test_a_value_that_does_not_fit_fails_the_row(drain, notion, captured_signals):
    ticket = Ticket.objects.create(title="A", priority="low,high")
    item = ticket.enqueue_notion_sync()

    assert drain() == {"failed": 1}

    item.refresh_from_db()
    assert "commas" in item.last_error
    assert captured_signals[0][0] == "sync_failed"


# --- Auto-sync ----------------------------------------------------------------------


def test_saves_queue_nothing_unless_auto_sync_is_on(no_network):
    ticket = Ticket.objects.create(title="A")
    ticket.title = "B"
    ticket.save()

    assert rows() == []


def test_auto_sync_queues_only_when_an_outbound_field_changed(no_network, auto_sync):
    ticket = Ticket.objects.create(title="A")
    [created] = rows(ticket)
    OutboxItem.objects.all().delete()

    ticket.save()  # Nothing changed.
    ticket.notes = "not mapped"
    ticket.save()
    ticket.status = "Done"
    ticket.save(update_fields=["status"])  # Notion owns it.
    ticket.title = "B"
    ticket.save(update_fields=["notes"])  # Title changed but was not written.
    assert rows() == []

    ticket.save(update_fields=["title"])
    assert len(rows(ticket)) == 1
    OutboxItem.objects.all().delete()

    reloaded = Ticket.objects.get(pk=ticket.pk)
    reloaded.tags.append("web")  # A change in place is still a change.
    reloaded.save()
    assert len(rows(ticket)) == 1


def test_auto_sync_notices_a_new_file(no_network, auto_sync, storage):
    ticket = Ticket.objects.create(title="A")
    OutboxItem.objects.all().delete()

    ticket.screenshot.save("shot.png", ContentFile(b"png"))

    assert len(rows(ticket)) == 1


def test_auto_sync_ignores_deferred_fields_it_did_not_load(no_network, auto_sync):
    ticket = Ticket.objects.create(title="A")
    OutboxItem.objects.all().delete()

    partial = Ticket.objects.only("pk", "notes").get(pk=ticket.pk)
    partial.notes = "x"
    partial.save()

    assert rows() == []


def test_auto_sync_with_unwatchable_sources_counts_every_save(no_network, auto_sync):
    note = Note.objects.create(text="A")
    OutboxItem.objects.all().delete()

    note.save()
    assert len(rows(note)) == 1
    OutboxItem.objects.all().delete()

    note.notion_page_id = "page-1"
    note.save(update_fields=["notion_page_id"])  # Link fields never count.
    assert rows() == []


def test_auto_sync_skips_fixture_loading(no_network, auto_sync):
    Ticket(title="A").save_base(raw=True)

    assert rows() == []


def test_a_rolled_back_save_queues_nothing(no_network, auto_sync):
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            Ticket.objects.create(title="A")
            raise RuntimeError("roll back")

    assert rows() == []


# --- Deletes ------------------------------------------------------------------


def linked_ticket(notion, title="A"):
    ticket = Ticket.objects.create(title=title)
    ticket.sync_to_notion()
    return ticket


def test_delete_does_nothing_in_notion_by_default(drain, notion):
    ticket = linked_ticket(notion)
    page_id = ticket.notion_page_id

    ticket.delete()

    assert rows() == []
    assert notion.pages[page_id]["in_trash"] is False


def test_delete_archives_the_linked_page(drain, notion, archive_on_delete):
    ticket = linked_ticket(notion)
    page_id = ticket.notion_page_id

    ticket.delete()

    [item] = rows()
    assert (item.operation, item.page_id) == ("archive", page_id)
    assert drain() == {"succeeded": 1}
    assert notion.pages[page_id]["in_trash"] is True


def test_a_queryset_delete_archives_each_page(drain, notion, archive_on_delete):
    pages = [linked_ticket(notion, title).notion_page_id for title in "AB"]

    Ticket.objects.all().delete()

    assert drain() == {"succeeded": 2}
    assert all(notion.pages[page_id]["in_trash"] for page_id in pages)


def test_delete_turns_a_pending_sync_into_the_archive(drain, notion, archive_on_delete):
    ticket = linked_ticket(notion)
    page_id = ticket.notion_page_id
    queued = ticket.enqueue_notion_sync()

    ticket.delete()

    [item] = rows()
    assert item.pk == queued.pk
    assert item.operation == "archive"
    assert drain() == {"succeeded": 1}
    assert notion.pages[page_id]["in_trash"] is True
    assert notion.count("PATCH", "/pages/") == 1


def test_delete_of_a_record_never_sent_queues_nothing(no_network, archive_on_delete):
    Ticket.objects.create(title="A").delete()

    assert rows() == []


def test_delete_finds_a_page_a_failed_create_left_unlinked(
    drain, notion, archive_on_delete
):
    ticket = Ticket.objects.create(title="A")
    item = ticket.enqueue_notion_sync()
    # The create reached Notion but the response was lost.
    notion.fail_next("POST", "/pages", httpx.ReadTimeout("lost"), apply=True)
    assert drain() == {"pending": 1}
    [page_id] = notion.pages

    ticket.delete()

    item.refresh_from_db()
    assert (item.operation, item.page_id) == ("archive", "")
    assert item.external_id == f"testapp.ticket:{item.object_id}"
    make_due(item)
    assert drain() == {"succeeded": 1}
    assert notion.pages[page_id]["in_trash"] is True
    assert len(notion.pages) == 1


def test_delete_before_the_first_send_creates_nothing(drain, notion, archive_on_delete):
    ticket = Ticket.objects.create(title="A")
    ticket.enqueue_notion_sync()

    ticket.delete()

    assert drain() == {"skipped": 1}
    assert notion.pages == {}
    assert "No page has external ID" in rows()[0].last_error


def test_delete_without_external_ids_skips_an_unsent_sync(
    drain, notion, archive_on_delete
):
    note = Note.objects.create(text="A")
    note.enqueue_notion_sync()

    note.delete()

    assert drain() == {"skipped": 1}
    assert notion.requests == []


# --- One record, one row in flight --------------------------------------------


def test_a_record_waits_while_its_earlier_row_is_being_sent(no_network, auto_sync):
    ticket = Ticket.objects.create(title="A")
    [first] = outbox.claim()
    ticket.title = "B"
    ticket.save()
    [second] = rows(ticket, status=Status.PENDING)

    assert outbox.claim() == []

    OutboxItem.objects.filter(pk=first.pk).update(status=Status.SUCCEEDED)
    assert [item.pk for item in outbox.claim()] == [second.pk]


def test_a_row_needing_review_holds_back_the_records_new_rows(drain, notion, auto_sync):
    note = Note.objects.create(text="A")
    notion.fail_next("POST", "/pages", httpx.ReadTimeout("lost"))
    assert drain() == {"needs_review": 1}

    note.text = "B"
    note.save()

    assert drain() == {}
    assert notion.count("POST", "/pages") == 1


def test_other_records_are_not_held_back(drain, notion, auto_sync):
    Note.objects.create(text="A")
    notion.fail_next("POST", "/pages", httpx.ReadTimeout("lost"))
    assert drain() == {"needs_review": 1}

    Note.objects.create(text="B")

    assert drain() == {"succeeded": 1}


def test_a_failed_row_gives_way_to_a_newer_one(notion, auto_sync):
    ticket = Ticket.objects.create(title="A")
    [first] = outbox.claim()
    ticket.title = "B"
    ticket.save()
    notion.fail_next("POST", "/query", error_response(503, "service_unavailable"))
    notion.fail_next("POST", "/pages", error_response(503, "service_unavailable"))

    outbox.send(first)

    first.refresh_from_db()
    assert first.status == Status.SKIPPED
    assert first.last_error == outbox.SUPERSEDED
    [newer] = rows(ticket, status=Status.PENDING)
    assert newer.pk != first.pk


def test_retry_gives_way_to_a_newer_row(no_network, auto_sync):
    ticket = Ticket.objects.create(title="A")
    [first] = rows()
    OutboxItem.objects.filter(pk=first.pk).update(status=Status.FAILED)
    ticket.title = "B"
    ticket.save()
    first.refresh_from_db()

    assert outbox.retry(first) is False
    assert first.status == Status.SKIPPED
    assert len(rows(ticket, status=Status.PENDING)) == 1


def test_an_interrupted_sync_is_released_by_whether_it_could_duplicate(no_network):
    ticket = Ticket.objects.create(title="A")
    unguarded = Note.objects.create(text="A")
    linked = Note.objects.create(text="B", notion_page_id="page-1")
    for record in (ticket, unguarded, linked):
        record.enqueue_notion_sync()
    outbox.claim()
    OutboxItem.objects.update(locked_at=timezone.now() - timedelta(minutes=11))

    assert outbox.release_stale() == 3

    [ticket_row] = rows(ticket)
    [unguarded_row] = rows(unguarded)
    [linked_row] = rows(linked)
    assert ticket_row.status == Status.PENDING  # Found by external ID next time.
    assert unguarded_row.status == Status.NEEDS_REVIEW
    assert linked_row.status == Status.PENDING  # An update cannot duplicate.


# --- Lost responses -----------------------------------------------------------


def test_a_lost_response_on_a_model_create_yields_one_page(drain, notion):
    ticket = Ticket.objects.create(title="A")
    ticket.enqueue_notion_sync()
    notion.fail_next(
        "POST", "/pages", error_response(504, "gateway_timeout"), apply=True
    )

    assert drain() == {"pending": 1}
    make_due(rows()[0])
    assert drain() == {"succeeded": 1}

    [page_id] = notion.pages
    ticket.refresh_from_db()
    assert ticket.notion_page_id == page_id


def test_without_external_ids_a_lost_create_needs_review(drain, notion):
    note = Note.objects.create(text="A")
    note.enqueue_notion_sync()
    notion.fail_next("POST", "/pages", httpx.ReadTimeout("lost"), apply=True)

    assert drain() == {"needs_review": 1}
    assert len(notion.pages) == 1


def test_without_external_ids_a_lost_update_is_retried(drain, notion):
    note = Note.objects.create(text="A")
    note.sync_to_notion()
    note.enqueue_notion_sync()
    notion.fail_next("PATCH", "/pages/", httpx.ReadTimeout("lost"))

    assert drain() == {"pending": 1}


# --- Concurrency (Postgres) ---------------------------------------------------


@pytest.mark.skipif(
    django_settings.TEST_DB != "postgres", reason="needs row locks (Postgres)"
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_enqueues_share_one_pending_row():
    ticket = Ticket.objects.create(title="A")
    barrier = threading.Barrier(4)
    made = []

    def enqueue():
        try:
            barrier.wait(timeout=5)
            made.append(Ticket.objects.get(pk=ticket.pk).enqueue_notion_sync().pk)
        finally:
            connection.close()

    threads = [threading.Thread(target=enqueue) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(made) == 4
    assert len(set(made)) == 1
    assert OutboxItem.objects.filter(status=Status.PENDING).count() == 1
