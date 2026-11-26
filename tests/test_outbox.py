import logging
import threading
from datetime import date, timedelta
from io import BytesIO

import httpx
import pytest
from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import ContentFile
from django.db import connection, transaction
from django.utils import timezone

from notion_sync import api, outbox
from notion_sync.exceptions import NotionValidationError
from notion_sync.files import NotionFile
from notion_sync.models import OutboxItem
from tests.fake_notion import error_response
from tests.settings import INBOX_TOKEN

pytestmark = pytest.mark.django_db

Status = OutboxItem.Status


def make_due(item):
    """Pretend the backoff has passed."""
    OutboxItem.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now())


def text(content):
    return {"rich_text": [{"type": "text", "text": {"content": content}}]}


# --- Enqueue ---


def test_enqueue_stores_a_snapshot_without_calling_notion(no_network):
    item = api.enqueue_create(
        "inbox", {"Name": "A", "Due": date(2026, 12, 1), "Tags": ["x"]}
    )

    item.refresh_from_db()
    assert item.status == Status.PENDING
    assert item.operation == "create"
    assert item.target == "inbox"
    assert item.attempts == 0
    assert item.payload == {
        "Name": "A",
        "Due": {"__notion_sync__": "date", "value": "2026-12-01"},
        "Tags": ["x"],
    }


def test_enqueue_variants_store_their_identifiers(no_network):
    update = api.enqueue_update("inbox", "page-1", {"Name": "A"})
    upsert = api.enqueue_upsert("inbox", "order-17", {"Name": "A"})
    archive = api.enqueue_archive("inbox", "page-2")

    assert (update.operation, update.page_id) == ("update", "page-1")
    assert (upsert.operation, upsert.external_id) == ("upsert", "order-17")
    assert (archive.operation, archive.page_id, archive.payload) == (
        "archive",
        "page-2",
        {},
    )


def test_a_queued_file_stores_its_path_not_its_bytes(no_network, storage):
    storage.save("shot.png", BytesIO(b"png-bytes"))

    item = api.enqueue_create("inbox", {"Name": "A", "Files": NotionFile("shot.png")})

    assert item.payload["Files"] == {
        "__notion_sync__": "file",
        "value": {
            "path": "shot.png",
            "storage": "default",
            "name": "shot.png",
            "content_type": "image/png",
        },
    }


def test_enqueue_rejects_what_it_cannot_store_or_send(no_network):
    with pytest.raises(ImproperlyConfigured):
        api.enqueue_create("nope", {"Name": "A"})
    with pytest.raises(NotionValidationError, match="EXTERNAL_ID_PROPERTY"):
        api.enqueue_create("plain", {"Name": "A"}, external_id="order-17")
    with pytest.raises(NotionValidationError, match="EXTERNAL_ID_PROPERTY"):
        api.enqueue_upsert("plain", "order-17", {"Name": "A"})
    with pytest.raises(NotionValidationError, match="page ID"):
        api.enqueue_update("inbox", "", {"Name": "A"})
    with pytest.raises(NotionValidationError, match="cannot be queued"):
        api.enqueue_create("inbox", {"Name": object()})
    with pytest.raises(NotionValidationError, match="must come from a storage"):
        api.enqueue_create(
            "inbox", {"Files": NotionFile(ContentFile(b"x", name="a.txt"))}
        )

    assert OutboxItem.objects.count() == 0


# --- Drain: success ---


def test_drain_sends_a_queued_create(drain, notion, captured_signals):
    item = api.enqueue_create("plain", {"Name": "A", "Due": date(2026, 12, 1)})

    counts = drain()

    item.refresh_from_db()
    assert counts == {"succeeded": 1}
    assert item.status == Status.SUCCEEDED
    assert item.attempts == 1
    assert item.last_error == ""
    assert item.locked_at is None
    [page] = notion.pages.values()
    assert item.result_page_id == page["id"]
    assert page["properties"]["Due"]["date"]["start"] == "2026-12-01"
    [(name, sent)] = captured_signals
    assert name == "page_synced"
    assert sent["item"] == item
    assert sent["operation"] == "create"
    assert sent["page_id"] == page["id"]


def test_a_queued_create_carries_the_row_id_as_its_external_id(drain, notion):
    item = api.enqueue_create("inbox", {"Name": "A"})

    drain()

    [page] = notion.pages.values()
    assert page["properties"]["External ID"]["rich_text"][0]["plain_text"] == str(
        item.id
    )


def test_a_queued_create_keeps_the_callers_external_id(drain, notion):
    api.enqueue_create("inbox", {"Name": "A"}, external_id="order-17")

    drain()

    [page] = notion.pages.values()
    assert page["properties"]["External ID"]["rich_text"][0]["plain_text"] == "order-17"


def test_drain_sends_updates_upserts_and_archives(drain, notion):
    first = notion.add_page({"Name": {"title": []}})
    second = notion.add_page({"External ID": text("order-17")})
    third = notion.add_page({})
    api.enqueue_update("inbox", first["id"], {"Number": 5})
    api.enqueue_upsert("inbox", "order-17", {"Done": True})
    api.enqueue_archive("inbox", third["id"])

    counts = drain()

    assert counts == {"succeeded": 3}
    assert first["properties"]["Number"]["number"] == 5
    assert second["properties"]["Done"]["checkbox"] is True
    assert third["in_trash"] is True
    assert len(notion.pages) == 3


def test_drain_uploads_a_queued_file(drain, notion, storage):
    storage.save("shot.png", BytesIO(b"png-bytes"))
    api.enqueue_create("plain", {"Name": "A", "Files": NotionFile("shot.png")})

    assert drain() == {"succeeded": 1}

    [upload] = notion.uploads.values()
    assert b"png-bytes" in upload["parts"][0]


def test_drain_respects_the_limit_and_leaves_future_rows(drain, notion):
    for name in "ABC":
        api.enqueue_create("plain", {"Name": name})
    later = api.enqueue_create("plain", {"Name": "Later"})
    OutboxItem.objects.filter(pk=later.pk).update(
        next_attempt_at=timezone.now() + timedelta(hours=1)
    )

    assert drain(limit=2) == {"succeeded": 2}
    assert drain() == {"succeeded": 1}
    assert drain() == {}
    later.refresh_from_db()
    assert later.status == Status.PENDING


# --- Drain: failures ---


def test_backoff_doubles_from_thirty_seconds_to_an_hour():
    assert [outbox.backoff_seconds(n) for n in range(1, 10)] == [
        30,
        60,
        120,
        240,
        480,
        960,
        1920,
        3600,
        3600,
    ]


@pytest.mark.parametrize(
    "outage",
    [
        error_response(503, "service_unavailable"),
        error_response(500, "internal_server_error"),
        error_response(409, "conflict_error"),
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
    ],
)
def test_an_outage_leaves_the_row_pending_and_the_next_drain_sends_it(
    drain, notion, captured_signals, outage
):
    page = notion.add_page({})
    item = api.enqueue_update("inbox", page["id"], {"Number": 5})
    notion.fail_next("PATCH", "/pages/", outage)

    before = timezone.now()
    assert drain() == {"pending": 1}

    item.refresh_from_db()
    assert item.status == Status.PENDING
    assert item.attempts == 1
    assert item.last_error
    wait = (item.next_attempt_at - before).total_seconds()
    assert 30 <= wait < 35
    assert captured_signals == []
    # Not due yet: another drain right away does nothing.
    assert drain() == {}

    make_due(item)
    assert drain() == {"succeeded": 1}
    assert page["properties"]["Number"]["number"] == 5


def test_an_outage_while_fetching_the_schema_also_leaves_the_row_pending(drain, notion):
    item = api.enqueue_create("plain", {"Name": "A"})
    notion.fail_next("GET", "/data_sources/", httpx.ConnectError("refused"))

    assert drain() == {"pending": 1}
    make_due(item)
    assert drain() == {"succeeded": 1}
    assert len(notion.pages) == 1


def test_the_wait_grows_with_each_failed_attempt(drain, notion):
    page = notion.add_page({})
    item = api.enqueue_update("inbox", page["id"], {"Number": 5})
    notion.fail_next(
        "PATCH", "/pages/", error_response(503, "service_unavailable"), times=3
    )

    waits = []
    for _ in range(3):
        make_due(item)
        before = timezone.now()
        drain()
        item.refresh_from_db()
        waits.append(round((item.next_attempt_at - before).total_seconds(), -1))

    assert waits == [30, 60, 120]


def test_rate_limit_honours_retry_after(drain, notion):
    page = notion.add_page({})
    item = api.enqueue_update("inbox", page["id"], {"Number": 5})
    notion.fail_next(
        "PATCH",
        "/pages/",
        error_response(429, "rate_limited", headers={"Retry-After": "300"}),
    )

    before = timezone.now()
    drain()

    item.refresh_from_db()
    assert item.status == Status.PENDING
    assert 300 <= (item.next_attempt_at - before).total_seconds() < 305


def test_a_row_rescheduled_for_now_is_not_sent_twice_in_one_drain(
    drain, notion, monkeypatch
):
    monkeypatch.setattr(outbox, "MIN_RETRY_SECONDS", 0)
    page = notion.add_page({})
    api.enqueue_update("inbox", page["id"], {"Number": 5})
    notion.fail_next(
        "PATCH",
        "/pages/",
        error_response(429, "rate_limited", headers={"Retry-After": "0"}),
        times=5,
    )

    assert drain() == {"pending": 1}
    assert notion.count("PATCH", "/pages/") == 1


def test_after_the_attempt_cap_the_row_fails_and_a_signal_fires(
    drain, notion, settings, captured_signals
):
    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "MAX_ATTEMPTS": 3}
    page = notion.add_page({})
    item = api.enqueue_update("inbox", page["id"], {"Number": 5})
    notion.fail_next(
        "PATCH", "/pages/", error_response(503, "service_unavailable"), times=10
    )

    results = []
    for _ in range(4):
        make_due(item)
        results.append(dict(drain()))

    item.refresh_from_db()
    assert results == [{"pending": 1}, {"pending": 1}, {"failed": 1}, {}]
    assert item.status == Status.FAILED
    assert item.attempts == 3
    assert "Gave up after 3 attempts" in item.last_error
    assert notion.count("PATCH", "/pages/") == 3
    [(name, sent)] = captured_signals
    assert name == "sync_failed"
    assert sent["item"] == item
    assert "503" in sent["error"]


@pytest.mark.parametrize(
    "rejection",
    [
        error_response(400, "validation_error"),
        error_response(401, "unauthorized"),
        error_response(403, "restricted_resource"),
        error_response(404, "object_not_found"),
    ],
)
def test_validation_and_permission_errors_fail_at_once(
    drain, notion, captured_signals, rejection
):
    page = notion.add_page({})
    item = api.enqueue_update("inbox", page["id"], {"Number": 5})
    notion.fail_next("PATCH", "/pages/", rejection)

    assert drain() == {"failed": 1}

    item.refresh_from_db()
    assert item.status == Status.FAILED
    assert item.attempts == 1
    assert [name for name, _ in captured_signals] == ["sync_failed"]


def test_a_value_that_does_not_fit_the_schema_fails_at_once(
    drain, notion, captured_signals
):
    item = api.enqueue_create("plain", {"Name": "A", "Number": "five"})

    assert drain() == {"failed": 1}

    item.refresh_from_db()
    assert '"Number"' in item.last_error
    assert notion.count("POST", "/pages") == 0
    assert [name for name, _ in captured_signals] == ["sync_failed"]


def test_a_removed_target_fails_the_row(drain, notion, settings):
    item = api.enqueue_create("plain", {"Name": "A"})
    settings.NOTION_SYNC = {"TARGETS": {}}

    assert drain() == {"failed": 1}

    item.refresh_from_db()
    assert "no target named 'plain'" in item.last_error


def test_an_unexpected_error_parks_the_row_and_the_drain_continues(
    drain, notion, monkeypatch, captured_signals
):
    broken = api.enqueue_create("plain", {"Name": "A"})
    fine = api.enqueue_create("plain", {"Name": "B"})
    real = outbox.perform_item

    def perform(item, target, client):
        if item.pk == broken.pk:
            raise RuntimeError("a bug")
        return real(item, target, client)

    monkeypatch.setattr(outbox, "perform_item", perform)

    assert drain() == {"failed": 1, "succeeded": 1}

    broken.refresh_from_db()
    fine.refresh_from_db()
    assert broken.status == Status.FAILED
    assert "RuntimeError: a bug" in broken.last_error
    assert fine.status == Status.SUCCEEDED


def test_tokens_stay_out_of_rows_and_logs(drain, notion, caplog):
    caplog.set_level(logging.DEBUG)
    page = notion.add_page({})
    api.enqueue_update("inbox", page["id"], {"Number": 5})
    api.enqueue_update("inbox", page["id"], {"Number": 6})
    api.enqueue_update("inbox", page["id"], {"Number": "bad"})
    notion.fail_next("PATCH", "/pages/", error_response(401, "unauthorized"))
    notion.fail_next("PATCH", "/pages/", httpx.ReadTimeout("slow"))

    drain()

    for item in OutboxItem.objects.all():
        assert item.last_error
        assert INBOX_TOKEN not in item.last_error
        assert INBOX_TOKEN not in str(item.payload)
    assert INBOX_TOKEN not in caplog.text


# --- The duplicate guard ---


def test_a_lost_response_on_create_yields_one_page(drain, notion, captured_signals):
    item = api.enqueue_create("inbox", {"Name": "A"})
    # Notion creates the page, but the response never arrives.
    notion.fail_next("POST", "/pages", httpx.ReadTimeout("slow"), apply=True)

    assert drain() == {"pending": 1}
    assert len(notion.pages) == 1

    make_due(item)
    assert drain() == {"succeeded": 1}

    item.refresh_from_db()
    [page] = notion.pages.values()
    assert item.status == Status.SUCCEEDED
    assert item.result_page_id == page["id"]
    assert notion.count("POST", "/pages") == 1
    assert notion.count("POST", "/query") == 1
    assert [name for name, _ in captured_signals] == ["page_synced"]


def test_a_lost_500_on_create_also_yields_one_page(drain, notion):
    item = api.enqueue_create("inbox", {"Name": "A"}, external_id="order-17")
    notion.fail_next(
        "POST", "/pages", error_response(500, "internal_server_error"), apply=True
    )

    drain()
    make_due(item)
    drain()

    assert len(notion.pages) == 1
    assert notion.count("POST", "/pages") == 1


def test_a_create_that_never_reached_notion_is_sent_again(drain, notion):
    item = api.enqueue_create("inbox", {"Name": "A"})
    notion.fail_next("POST", "/pages", httpx.ReadTimeout("slow"))  # Not applied.

    drain()
    make_due(item)
    assert drain() == {"succeeded": 1}

    assert len(notion.pages) == 1
    assert notion.count("POST", "/pages") == 2


def test_the_first_attempt_does_not_query(drain, notion):
    api.enqueue_create("inbox", {"Name": "A"})

    drain()

    assert notion.count("POST", "/query") == 0


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("slow"), error_response(502, "bad_gateway")],
)
def test_without_an_external_id_an_uncertain_create_needs_review(
    drain, notion, captured_signals, failure
):
    item = api.enqueue_create("plain", {"Name": "A"})
    notion.fail_next("POST", "/pages", failure, apply=True)

    assert drain() == {"needs_review": 1}

    item.refresh_from_db()
    assert item.status == Status.NEEDS_REVIEW
    assert "may or may not exist" in item.last_error
    [(name, sent)] = captured_signals
    assert name == "sync_failed"
    assert sent["item"].status == Status.NEEDS_REVIEW

    # It is never sent again by itself.
    make_due(item)
    assert drain() == {}
    assert notion.count("POST", "/pages") == 1
    assert len(notion.pages) == 1


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("refused"),
        error_response(429, "rate_limited"),
        error_response(409, "conflict_error"),
    ],
)
def test_without_an_external_id_a_clear_failure_is_retried(drain, notion, failure):
    item = api.enqueue_create("plain", {"Name": "A"})
    notion.fail_next("POST", "/pages", failure)

    assert drain() == {"pending": 1}
    make_due(item)
    assert drain() == {"succeeded": 1}
    assert len(notion.pages) == 1


# --- Rows left in "sending" ---


def stuck(item, *, minutes):
    OutboxItem.objects.filter(pk=item.pk).update(
        status=Status.SENDING,
        attempts=1,
        locked_at=timezone.now() - timedelta(minutes=minutes),
    )


def test_an_interrupted_create_with_the_guard_is_looked_up_then_sent(drain, notion):
    item = api.enqueue_create("inbox", {"Name": "A"})
    stuck(item, minutes=11)

    assert outbox.release_stale() == 1
    assert drain() == {"succeeded": 1}

    assert notion.count("POST", "/query") == 1
    assert len(notion.pages) == 1


def test_an_interrupted_create_without_the_guard_needs_review(
    drain, notion, captured_signals
):
    item = api.enqueue_create("plain", {"Name": "A"})
    stuck(item, minutes=11)

    assert drain() == {}

    item.refresh_from_db()
    assert item.status == Status.NEEDS_REVIEW
    assert notion.requests == []
    assert [name for name, _ in captured_signals] == ["sync_failed"]


def test_an_interrupted_update_goes_back_to_pending(drain, notion):
    page = notion.add_page({})
    item = api.enqueue_update("plain", page["id"], {"Number": 5})
    stuck(item, minutes=11)

    assert drain() == {"succeeded": 1}


def test_a_row_still_being_sent_is_left_alone(drain, notion):
    item = api.enqueue_create("inbox", {"Name": "A"})
    stuck(item, minutes=9)

    assert outbox.release_stale() == 0
    assert drain() == {}
    item.refresh_from_db()
    assert item.status == Status.SENDING


# --- Claiming ---


def test_claim_marks_rows_as_sending_and_counts_the_attempt(no_network):
    first = api.enqueue_create("plain", {"Name": "A"})
    api.enqueue_create("plain", {"Name": "B"})

    claimed = outbox.claim(1)

    assert [item.pk for item in claimed] == [first.pk]
    first.refresh_from_db()
    assert (first.status, first.attempts) == (Status.SENDING, 1)
    assert first.locked_at is not None
    assert (claimed[0].status, claimed[0].attempts) == (Status.SENDING, 1)
    assert OutboxItem.objects.filter(status=Status.PENDING).count() == 1


@pytest.mark.skipif(
    django_settings.TEST_DB != "postgres", reason="needs row locks (Postgres)"
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_claims_skip_rows_locked_by_another_drain():
    for index in range(8):
        api.enqueue_create("plain", {"Name": str(index)})
    ordered = OutboxItem.objects.order_by("next_attempt_at", "created_at")
    claimed = []

    def other_drain():
        try:
            claimed.extend(item.pk for item in outbox.claim(5))
        finally:
            connection.close()

    # This transaction plays a drain that has locked the first five rows and
    # not yet committed. A second drain must take other rows without waiting.
    with transaction.atomic():
        locked = [item.pk for item in ordered.select_for_update()[:5]]
        thread = threading.Thread(target=other_drain)
        thread.start()
        thread.join(timeout=5)
        waited = thread.is_alive()
    thread.join(timeout=5)

    assert not waited, "claim() blocked on rows locked by another transaction"
    assert len(claimed) == 3
    assert not set(claimed) & set(locked)


# --- Prune ---


def test_prune_deletes_old_successes_and_skips_only(no_network):
    old = timezone.now() - timedelta(days=8)
    recent = timezone.now() - timedelta(days=6)
    rows = {}
    for label, status, updated in [
        ("old success", Status.SUCCEEDED, old),
        ("old skip", Status.SKIPPED, old),
        ("recent success", Status.SUCCEEDED, recent),
        ("old failure", Status.FAILED, old),
        ("old review", Status.NEEDS_REVIEW, old),
        ("old pending", Status.PENDING, old),
    ]:
        item = api.enqueue_create("plain", {"Name": label})
        OutboxItem.objects.filter(pk=item.pk).update(status=status, updated_at=updated)
        rows[label] = item.pk

    assert outbox.prune() == 2

    remaining = set(OutboxItem.objects.values_list("pk", flat=True))
    assert remaining == set(rows.values()) - {rows["old success"], rows["old skip"]}
    assert outbox.prune(days=5) == 1
