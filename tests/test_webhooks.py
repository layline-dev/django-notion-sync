import json
import logging
import threading
import uuid
from datetime import timedelta
from io import StringIO

import pytest
from django.conf import settings as django_settings
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.db import connection, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from notion_sync import tasks, webhooks
from notion_sync.models import OutboxItem, WebhookEvent
from notion_sync.signals import page_changed, page_trashed
from notion_sync.sync import deleting_for_notion_trash
from notion_sync.tasks import process_webhook_event, process_webhook_events
from tests.fake_notion import error_response
from tests.settings import INBOX_TOKEN, INBOX_WEBHOOK_TOKEN, PLAIN_WEBHOOK_TOKEN
from tests.testapp.models import Note, Ticket

pytestmark = pytest.mark.django_db

URL = "/notion/webhook/"
LATER = "2026-11-22T10:05:00.000Z"
EARLIER = "2026-11-22T09:00:00.000Z"


def status(name):
    return {"status": {"name": name}}


def make_event(
    page_id,
    event_type="page.properties_updated",
    *,
    data_source="ds-inbox",
    event_id=None,
    timestamp=None,
    parent=True,
):
    data = {"updated_properties": ["abc"]}
    if parent:
        data["parent"] = {"id": "db-1", "type": "database"}
        if data_source is not None:
            data["parent"]["data_source_id"] = data_source
    return {
        "id": event_id or str(uuid.uuid4()),
        "timestamp": (timestamp or timezone.now()).isoformat(),
        "workspace_id": "ws-1",
        "workspace_name": "Workspace",
        "subscription_id": "sub-1",
        "integration_id": "int-1",
        "type": event_type,
        "authors": [{"id": "user-1", "type": "person"}],
        "attempt_number": 1,
        "api_version": "2025-09-03",
        "entity": {"id": page_id, "type": "page"},
        "data": data,
    }


def deliver(client, payload, *, token=INBOX_WEBHOOK_TOKEN, signature=None):
    body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
    headers = {}
    if signature is not None:
        headers["X-Notion-Signature"] = signature
    elif token is not None:
        headers["X-Notion-Signature"] = webhooks.sign(body, token)
    return client.post(URL, data=body, content_type="application/json", headers=headers)


def linked_ticket(notion, value="Not started", **fields):
    page = notion.add_page({"Status": status(value)})
    ticket = Ticket.objects.create(title="A ticket", status=value, **fields)
    Ticket.objects.filter(pk=ticket.pk).update(
        notion_page_id=page["id"],
        notion_last_edited_time=page["last_edited_time"],
    )
    ticket.refresh_from_db()
    return ticket, page


@pytest.fixture
def inbound_signals():
    received = []

    def on_changed(sender, **kwargs):
        received.append(("page_changed", sender, kwargs))

    def on_trashed(sender, **kwargs):
        # A deleted instance loses its pk afterwards; note it now.
        kwargs["pk"] = getattr(kwargs["instance"], "pk", None)
        received.append(("page_trashed", sender, kwargs))

    page_changed.connect(on_changed)
    page_trashed.connect(on_trashed)
    yield received
    page_changed.disconnect(on_changed)
    page_trashed.disconnect(on_trashed)


def page_fetches(notion):
    return notion.find("GET", "/v1/pages/")


# --- Routing and the URL ---


def test_the_url_is_named():
    assert reverse("notion_sync:webhook") == URL


def test_only_post_is_allowed(client):
    assert client.get(URL).status_code == 405


def test_csrf_is_not_required(notion):
    strict = Client(enforce_csrf_checks=True)
    assert deliver(strict, {"verification_token": "secret_abc"}).status_code == 200


def test_a_body_that_is_not_json_is_rejected(client):
    response = client.post(URL, data=b"not json", content_type="application/json")
    assert response.status_code == 400
    response = client.post(URL, data=b"[1, 2]", content_type="application/json")
    assert response.status_code == 400


# --- The handshake ---


def test_the_handshake_token_is_held_for_the_token_command(client, caplog):
    caplog.set_level(logging.DEBUG)
    response = deliver(client, {"verification_token": "secret_handshake"}, token=None)

    assert response.status_code == 200
    out = StringIO()
    call_command("notion_sync_webhook_token", stdout=out)
    assert out.getvalue() == "secret_handshake\n"
    assert "secret_handshake" not in caplog.text


def test_the_token_command_warns_when_several_tokens_arrived(client):
    deliver(client, {"verification_token": "secret_one"}, token=None)
    deliver(client, {"verification_token": "secret_two"}, token=None)
    out, err = StringIO(), StringIO()

    call_command("notion_sync_webhook_token", stdout=out, stderr=err)

    lines = out.getvalue().splitlines()
    assert [line.split()[0] for line in lines] == ["secret_one", "secret_two"]
    assert "2 tokens arrived" in err.getvalue()


def test_handshake_tokens_expire_after_15_minutes(client, monkeypatch):
    deliver(client, {"verification_token": "secret_old"}, token=None)
    real = webhooks.time.time
    monkeypatch.setattr(webhooks.time, "time", lambda: real() + 15 * 60 + 1)

    with pytest.raises(CommandError, match="Resend token"):
        call_command("notion_sync_webhook_token", stdout=StringIO())


def test_an_old_token_expires_even_when_a_newer_one_arrived(client, monkeypatch):
    clock = [webhooks.time.time()]
    monkeypatch.setattr(webhooks.time, "time", lambda: clock[0])
    deliver(client, {"verification_token": "secret_old"}, token=None)
    clock[0] += 10 * 60
    deliver(client, {"verification_token": "secret_new"}, token=None)
    clock[0] += 6 * 60

    tokens = [token for token, _ in webhooks.recent_verification_tokens()]

    assert tokens == ["secret_new"]


def test_at_most_ten_handshake_tokens_are_kept(client):
    for index in range(12):
        deliver(client, {"verification_token": f"secret_{index}"}, token=None)

    tokens = [token for token, _ in webhooks.recent_verification_tokens()]

    assert tokens == [f"secret_{index}" for index in range(2, 12)]


def test_the_token_command_says_when_no_token_arrived():
    with pytest.raises(CommandError, match="No verification token"):
        call_command("notion_sync_webhook_token", stdout=StringIO())


def test_only_a_bare_token_body_counts_as_a_handshake(client):
    payload = {"verification_token": "secret_x", "type": "page.created"}
    assert deliver(client, payload, token=None).status_code == 401
    assert deliver(client, {"verification_token": ""}, token=None).status_code == 401
    assert webhooks.recent_verification_tokens() == []


# --- Signatures ---


def test_the_signature_matches_notions_format():
    body = b'{"a":1}'
    signature = webhooks.sign(body, "secret_key")
    assert signature.startswith("sha256=")
    assert len(signature) == len("sha256=") + 64


def test_a_missing_signature_is_rejected(client, notion):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited=LATER)

    response = deliver(client, make_event(page["id"]), token=None)

    assert response.status_code == 401
    assert page_fetches(notion) == []


def test_a_forged_signature_is_rejected(client, notion, caplog):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited=LATER)

    response = deliver(client, make_event(page["id"]), token="secret_wrong")

    assert response.status_code == 401
    assert page_fetches(notion) == []
    ticket.refresh_from_db()
    assert ticket.status == "Not started"
    assert INBOX_WEBHOOK_TOKEN not in caplog.text


def test_a_signature_for_a_different_body_is_rejected(client, notion):
    ticket, page = linked_ticket(notion)
    event = make_event(page["id"])
    signature = webhooks.sign(
        json.dumps({**event, "id": "other"}).encode(), INBOX_WEBHOOK_TOKEN
    )

    assert deliver(client, event, signature=signature).status_code == 401


def test_the_body_is_checked_as_sent_not_reserialized(client, notion):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited=LATER)
    # Spacing that json.dumps would not produce.
    body = json.dumps(make_event(page["id"]), indent=3).encode()

    response = deliver(client, body)

    assert response.status_code == 200
    ticket.refresh_from_db()
    assert ticket.status == "Done"


def test_an_event_signed_for_another_target_is_not_routed_to_this_one(client, notion):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited=LATER)

    # A valid signature from the "plain" target's subscription, naming the
    # "inbox" data source.
    response = deliver(client, make_event(page["id"]), token=PLAIN_WEBHOOK_TOKEN)

    assert response.status_code == 200
    assert page_fetches(notion) == []
    ticket.refresh_from_db()
    assert ticket.status == "Not started"


def test_targets_without_a_verification_token_accept_nothing(client, notion, settings):
    settings.NOTION_SYNC = {
        "TARGETS": {"inbox": {"TOKEN": "t", "DATA_SOURCE_ID": "ds-inbox"}}
    }
    page = notion.add_page()
    assert deliver(client, make_event(page["id"])).status_code == 401


# --- Applying an event ---


def test_a_status_change_updates_the_linked_record(client, notion, inbound_signals):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited=LATER)

    response = deliver(client, make_event(page["id"]))

    assert response.status_code == 200
    ticket.refresh_from_db()
    assert ticket.status == "Done"
    assert ticket.notion_last_edited_time.isoformat() == "2026-11-22T10:05:00+00:00"
    [(name, sender, kwargs)] = inbound_signals
    assert name == "page_changed"
    assert sender is Ticket
    assert kwargs["target"] == "inbox"
    assert kwargs["event_type"] == "page.properties_updated"
    assert kwargs["instance"].pk == ticket.pk
    assert kwargs["changed_fields"] == ["status"]
    assert kwargs["page"].properties["Status"] == "Done"


def test_applying_an_inbound_change_queues_no_outbound_sync(client, notion, settings):
    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "AUTO_SYNC": True}
    ticket, page = linked_ticket(notion)
    OutboxItem.objects.all().delete()
    notion.edit(page["id"], {"Status": status("Done")}, edited=LATER)

    deliver(client, make_event(page["id"]))

    ticket.refresh_from_db()
    assert ticket.status == "Done"
    assert OutboxItem.objects.count() == 0


def test_the_echo_of_an_outbound_write_changes_nothing(
    client, notion, settings, inbound_signals
):
    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "AUTO_SYNC": True}
    ticket = Ticket.objects.create(title="Echo")
    OutboxItem.objects.all().delete()
    notion.now = LATER
    page = ticket.sync_to_notion()
    notion.edit(page.id, {"Status": status("Not started")}, edited=LATER)
    Ticket.objects.filter(pk=ticket.pk).update(status="Not started")
    writes = notion.count("PATCH", "/v1/pages/") + notion.count("POST", "/v1/pages")

    response = deliver(client, make_event(page.id, "page.created"))

    assert response.status_code == 200
    assert inbound_signals == []
    assert OutboxItem.objects.count() == 0
    assert (
        notion.count("PATCH", "/v1/pages/") + notion.count("POST", "/v1/pages")
        == writes
    )


def test_a_replayed_event_is_ignored(client, notion, inbound_signals):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited=LATER)
    event = make_event(page["id"])

    assert deliver(client, event).status_code == 200
    Ticket.objects.filter(pk=ticket.pk).update(status="Changed locally")
    assert deliver(client, event).status_code == 200

    assert len(page_fetches(notion)) == 1
    ticket.refresh_from_db()
    assert ticket.status == "Changed locally"


def test_an_event_older_than_the_replay_window_is_ignored(client, notion):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited=LATER)
    old = timezone.now() - webhooks.REPLAY_WINDOW - timedelta(minutes=1)

    response = deliver(client, make_event(page["id"], timestamp=old))

    assert response.status_code == 200
    assert page_fetches(notion) == []


def test_seen_event_ids_are_kept_for_the_replay_window(client, notion, monkeypatch):
    page = notion.add_page()
    calls = []
    real = cache.add
    monkeypatch.setattr(
        webhooks.cache,
        "add",
        lambda key, value, timeout: calls.append(timeout) or real(key, value, timeout),
    )
    deliver(client, make_event(page["id"]))
    assert calls == [int(webhooks.REPLAY_WINDOW.total_seconds())]


@pytest.mark.parametrize(
    "event_type",
    ["page.content_updated", "page.locked", "comment.created", "data_source.created"],
)
def test_event_types_that_cannot_change_properties_are_ignored(
    client, notion, event_type
):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited=LATER)

    assert deliver(client, make_event(page["id"], event_type)).status_code == 200

    assert page_fetches(notion) == []


@pytest.mark.parametrize("event_type", ["page.created", "page.moved", "page.undeleted"])
def test_other_page_events_apply_the_page(client, notion, event_type):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited=LATER)

    deliver(client, make_event(page["id"], event_type))

    ticket.refresh_from_db()
    assert ticket.status == "Done"


def test_an_event_without_a_data_source_is_ignored_and_logged(client, notion, caplog):
    page = notion.add_page()

    for event in (
        make_event(page["id"], data_source=None),
        make_event(page["id"], parent=False),
    ):
        assert deliver(client, event).status_code == 200

    assert page_fetches(notion) == []
    assert "2025-09-03" in caplog.text


def test_an_event_for_an_unknown_data_source_is_ignored(client, notion):
    page = notion.add_page(data_source="ds-elsewhere")

    response = deliver(client, make_event(page["id"], data_source="ds-elsewhere"))

    assert response.status_code == 200
    assert page_fetches(notion) == []


def test_data_source_ids_match_with_or_without_dashes(client, notion, settings):
    ds = "2f26ee68-df30-4251-aad4-8ddc420cba3d"
    settings.NOTION_SYNC = {
        "TARGETS": {
            "inbox": {
                "TOKEN": "t",
                "DATA_SOURCE_ID": ds.replace("-", ""),
                "WEBHOOK_VERIFICATION_TOKEN": INBOX_WEBHOOK_TOKEN,
            }
        }
    }
    notion.schemas[ds] = notion.schemas["ds-inbox"]
    ticket, page = linked_ticket(notion)
    notion.pages[page["id"]]["parent"]["data_source_id"] = ds
    notion.edit(page["id"], {"Status": status("Done")}, edited=LATER)

    deliver(client, make_event(page["id"], data_source=ds))

    ticket.refresh_from_db()
    assert ticket.status == "Done"


def test_a_page_now_in_another_data_source_is_ignored(client, notion):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited=LATER)
    notion.pages[page["id"]]["parent"]["data_source_id"] = "ds-elsewhere"

    deliver(client, make_event(page["id"]))

    ticket.refresh_from_db()
    assert ticket.status == "Not started"


def test_a_stale_page_state_is_skipped(client, notion, inbound_signals):
    ticket, page = linked_ticket(notion)
    Ticket.objects.filter(pk=ticket.pk).update(
        notion_last_edited_time="2026-11-22T10:05:00Z"
    )
    notion.edit(page["id"], {"Status": status("Done")}, edited=EARLIER)

    deliver(client, make_event(page["id"]))

    ticket.refresh_from_db()
    assert ticket.status == "Not started"
    assert inbound_signals == []


def test_a_state_with_the_same_edit_time_is_applied(client, notion, inbound_signals):
    # Notion keeps edit times to the minute: a second edit in the same minute
    # has the same time and must not be lost.
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited=page["last_edited_time"])

    deliver(client, make_event(page["id"]))

    ticket.refresh_from_db()
    assert ticket.status == "Done"
    assert inbound_signals[0][2]["changed_fields"] == ["status"]


def test_a_newer_edit_time_alone_sends_page_changed(client, notion, inbound_signals):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], edited=LATER)  # Some unmapped property changed.

    deliver(client, make_event(page["id"]))

    ticket.refresh_from_db()
    assert ticket.notion_last_edited_time.isoformat() == "2026-11-22T10:05:00+00:00"
    assert inbound_signals[0][2]["changed_fields"] == []


def test_an_event_for_an_unlinked_page_sends_page_changed(
    client, notion, inbound_signals
):
    page = notion.add_page({"Status": status("Done")})

    deliver(client, make_event(page["id"]))

    [(name, sender, kwargs)] = inbound_signals
    assert (name, sender, kwargs["instance"]) == ("page_changed", None, None)


def test_a_page_that_is_gone_is_skipped(client, notion, inbound_signals):
    response = deliver(client, make_event(str(uuid.uuid4())))

    assert response.status_code == 200
    assert inbound_signals == []


def test_a_notion_outage_asks_notion_to_deliver_again(client, notion):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited=LATER)
    notion.fail_next("GET", "/v1/pages/", error_response(502, "bad_gateway"))
    event = make_event(page["id"])

    assert deliver(client, event).status_code == 503
    ticket.refresh_from_db()
    assert ticket.status == "Not started"

    # Notion's retry of the same event is processed.
    assert deliver(client, event).status_code == 200
    ticket.refresh_from_db()
    assert ticket.status == "Done"


def test_a_permission_error_is_logged_and_acknowledged(client, notion, caplog):
    ticket, page = linked_ticket(notion)
    notion.fail_next("GET", "/v1/pages/", error_response(403, "restricted_resource"))

    assert deliver(client, make_event(page["id"])).status_code == 200
    assert "Could not process webhook event" in caplog.text


def test_an_unexpected_error_answers_500_and_allows_a_retry(
    client, notion, monkeypatch
):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited=LATER)
    event = make_event(page["id"])

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(webhooks.inbound, "process_page_event", boom)
    assert deliver(client, event).status_code == 500
    monkeypatch.undo()

    assert deliver(client, event).status_code == 200
    ticket.refresh_from_db()
    assert ticket.status == "Done"


def test_no_token_reaches_a_log_line(client, notion, caplog):
    caplog.set_level(logging.DEBUG)
    ticket, page = linked_ticket(notion)
    notion.fail_next("GET", "/v1/pages/", error_response(502, "bad_gateway"))
    deliver(client, make_event(page["id"]))
    deliver(client, make_event(page["id"]), token="secret_wrong")
    deliver(client, make_event(page["id"], "comment.created"))

    for secret in (INBOX_TOKEN, INBOX_WEBHOOK_TOKEN, PLAIN_WEBHOOK_TOKEN):
        assert secret not in caplog.text


# --- Trash ---


def trash(notion, page):
    notion.edit(page["id"], in_trash=True, edited=LATER)


@pytest.mark.parametrize("on_notion_trash", ["nothing", "unlink", "delete"])
def test_a_trashed_page_sends_page_trashed_and_applies_the_option(
    client, notion, settings, inbound_signals, on_notion_trash
):
    settings.NOTION_SYNC = {
        **settings.NOTION_SYNC,
        "ON_NOTION_TRASH": on_notion_trash,
        "ON_DELETE": "archive",
    }
    ticket, page = linked_ticket(notion)
    trash(notion, page)

    deliver(client, make_event(page["id"], "page.deleted"))

    [(name, sender, kwargs)] = inbound_signals
    assert (name, sender, kwargs["pk"]) == ("page_trashed", Ticket, ticket.pk)
    assert kwargs["page"].in_trash
    remaining = Ticket.objects.filter(pk=ticket.pk).first()
    if on_notion_trash == "nothing":
        assert remaining.notion_page_id == page["id"]
    elif on_notion_trash == "unlink":
        assert remaining.notion_page_id is None
        assert remaining.notion_last_edited_time is None
    else:
        assert remaining is None
    # The page is already in the trash: nothing is queued to archive it.
    assert OutboxItem.objects.count() == 0


def test_only_the_record_whose_page_was_trashed_skips_the_archive(settings):
    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "ON_DELETE": "archive"}
    note = Note.objects.create(text="other")
    Note.objects.filter(pk=note.pk).update(notion_page_id="page-other")
    note.refresh_from_db()
    ticket = Ticket.objects.create(title="trashed")

    with deleting_for_notion_trash(ticket):
        note.delete()

    assert OutboxItem.objects.filter(operation="archive").count() == 1


def test_a_trash_event_for_a_restored_page_applies_it_like_an_edit(
    client, notion, inbound_signals
):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited=LATER)

    deliver(client, make_event(page["id"], "page.deleted"))

    [(name, sender, kwargs)] = inbound_signals
    assert (name, kwargs["event_type"]) == ("page_changed", "page.deleted")
    ticket.refresh_from_db()
    assert (ticket.status, ticket.notion_page_id) == ("Done", page["id"])


def test_an_edit_event_for_a_trashed_page_does_nothing(client, notion, inbound_signals):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")})
    trash(notion, page)

    deliver(client, make_event(page["id"]))

    assert inbound_signals == []
    ticket.refresh_from_db()
    assert ticket.status == "Not started"


def test_a_trash_event_for_an_unlinked_page_sends_page_trashed(
    client, notion, inbound_signals
):
    page = notion.add_page()
    trash(notion, page)

    deliver(client, make_event(page["id"], "page.deleted"))

    [(name, sender, kwargs)] = inbound_signals
    assert (name, sender, kwargs["instance"]) == ("page_trashed", None, None)


# --- Stored processing ---


@pytest.fixture
def stored(settings):
    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "WEBHOOK_PROCESSING": "stored"}


@pytest.fixture(params=["drain", "process_webhooks", "celery"])
def process(request):
    """Process stored events through each entry point."""

    def run():
        if request.param == "celery":
            return process_webhook_events.apply().get()
        out = StringIO()
        call_command(f"notion_sync_{request.param}", stdout=out)
        return out.getvalue()

    return run


def test_a_stored_event_is_processed_later(client, notion, stored, process):
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited=LATER)

    assert deliver(client, make_event(page["id"])).status_code == 200
    ticket.refresh_from_db()
    assert ticket.status == "Not started"
    [event] = WebhookEvent.objects.all()
    assert (event.type, event.target, event.status) == (
        "page.properties_updated",
        "inbox",
        "pending",
    )
    assert event.entity_id == page["id"]

    process()

    event.refresh_from_db()
    assert event.status == "processed"
    assert event.processed_at is not None
    ticket.refresh_from_db()
    assert ticket.status == "Done"


def test_the_commands_report_stored_events(client, notion, stored):
    page = notion.add_page()
    deliver(client, make_event(page["id"]))
    out = StringIO()
    call_command("notion_sync_drain", stdout=out)
    assert out.getvalue() == "Nothing to send.\nWebhook events: 1 processed.\n"
    out = StringIO()
    call_command("notion_sync_process_webhooks", stdout=out)
    assert out.getvalue() == "No webhook events to process.\n"


def test_a_replayed_stored_event_is_stored_once(client, notion, stored):
    page = notion.add_page()
    event = make_event(page["id"])

    deliver(client, event)
    deliver(client, event)

    assert WebhookEvent.objects.count() == 1


def test_ignored_events_are_not_stored(client, notion, stored):
    page = notion.add_page()
    deliver(client, make_event(page["id"], "comment.created"))
    deliver(client, make_event(page["id"], data_source="ds-elsewhere"))
    deliver(client, make_event(page["id"]), token="secret_wrong")

    assert WebhookEvent.objects.count() == 0


def test_a_stored_event_retries_with_backoff_then_fails(
    client, notion, stored, settings
):
    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "MAX_ATTEMPTS": 2}
    page = notion.add_page()
    deliver(client, make_event(page["id"]))
    notion.fail_next("GET", "/v1/pages/", error_response(502, "bad_gateway"), times=2)

    webhooks.process_stored()
    event = WebhookEvent.objects.get()
    assert (event.status, event.attempts) == ("pending", 1)
    assert "502" in event.error
    assert event.next_attempt_at > timezone.now() + timedelta(seconds=20)

    WebhookEvent.objects.update(next_attempt_at=timezone.now())
    webhooks.process_stored()
    event.refresh_from_db()
    assert (event.status, event.attempts) == ("failed", 2)
    assert event.error.startswith("Gave up after 2 attempts")


def test_a_fatal_error_fails_a_stored_event_at_once(client, notion, stored):
    page = notion.add_page()
    deliver(client, make_event(page["id"]))
    notion.fail_next("GET", "/v1/pages/", error_response(403, "restricted_resource"))

    webhooks.process_stored()

    assert WebhookEvent.objects.get().status == "failed"


def test_a_stored_event_for_a_removed_target_fails(client, notion, stored, settings):
    page = notion.add_page()
    deliver(client, make_event(page["id"]))
    WebhookEvent.objects.update(target="removed")

    webhooks.process_stored()

    event = WebhookEvent.objects.get()
    assert event.status == "failed"
    assert "removed" in event.error


def test_an_interrupted_stored_event_goes_back_to_pending(client, notion, stored):
    page = notion.add_page()
    deliver(client, make_event(page["id"]))
    WebhookEvent.objects.update(
        status="processing", locked_at=timezone.now() - timedelta(hours=1)
    )

    webhooks.process_stored()

    assert WebhookEvent.objects.get().status == "processed"


def test_a_processing_event_is_not_taken_twice(client, notion, stored):
    page = notion.add_page()
    deliver(client, make_event(page["id"]))
    WebhookEvent.objects.update(status="processing", locked_at=timezone.now())

    assert webhooks.process_stored() == {}


def test_celery_dispatch_processes_a_stored_event_on_commit(
    client, notion, stored, settings, django_capture_on_commit_callbacks
):
    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "DISPATCH": "celery"}
    ticket, page = linked_ticket(notion)
    notion.edit(page["id"], {"Status": status("Done")}, edited=LATER)

    with django_capture_on_commit_callbacks(execute=True):
        deliver(client, make_event(page["id"]))

    assert WebhookEvent.objects.get().status == "processed"
    ticket.refresh_from_db()
    assert ticket.status == "Done"


def test_celery_dispatch_waits_for_the_commit(
    client, notion, stored, settings, django_capture_on_commit_callbacks
):
    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "DISPATCH": "celery"}
    page = notion.add_page()

    with django_capture_on_commit_callbacks() as callbacks:
        deliver(client, make_event(page["id"]))
        assert WebhookEvent.objects.get().status == "pending"

    assert len(callbacks) == 1
    callbacks[0]()
    assert WebhookEvent.objects.get().status == "processed"


def test_the_event_task_processes_only_its_event(client, notion, stored):
    page = notion.add_page()
    deliver(client, make_event(page["id"]))
    deliver(client, make_event(page["id"]))
    first, second = WebhookEvent.objects.order_by("pk")

    assert process_webhook_event.apply(args=[second.pk]).get() == "processed"

    first.refresh_from_db()
    assert first.status == "pending"


def test_a_stored_event_for_a_page_that_is_gone_is_processed(client, notion, stored):
    deliver(client, make_event(str(uuid.uuid4())))

    webhooks.process_stored()

    assert WebhookEvent.objects.get().status == "processed"


def test_a_broker_outage_leaves_the_event_for_the_drain(
    client, notion, stored, settings, django_capture_on_commit_callbacks, monkeypatch
):
    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "DISPATCH": "celery"}
    page = notion.add_page()

    def down(*args, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr(tasks.process_webhook_event, "delay", down)
    with django_capture_on_commit_callbacks(execute=True):
        assert deliver(client, make_event(page["id"])).status_code == 200

    assert WebhookEvent.objects.get().status == "pending"


def test_prune_keeps_events_for_the_replay_window(client, notion, stored):
    page = notion.add_page()
    for _ in range(3):
        deliver(client, make_event(page["id"]))
    webhooks.process_stored()
    old, recent, failed = WebhookEvent.objects.order_by("pk")
    WebhookEvent.objects.filter(pk=old.pk).update(
        received_at=timezone.now() - timedelta(hours=49)
    )
    WebhookEvent.objects.filter(pk=recent.pk).update(
        received_at=timezone.now() - timedelta(hours=47)
    )
    WebhookEvent.objects.filter(pk=failed.pk).update(
        status="failed", received_at=timezone.now() - timedelta(days=30)
    )

    assert webhooks.prune(days=0) == 1
    assert set(WebhookEvent.objects.values_list("pk", flat=True)) == {
        recent.pk,
        failed.pk,
    }


def test_the_admin_shows_stored_events_read_only(admin_client, client, notion, stored):
    page = notion.add_page()
    deliver(client, make_event(page["id"]))
    event = WebhookEvent.objects.get()
    change_url = f"/admin/notion_sync/webhookevent/{event.pk}/change/"

    assert admin_client.get("/admin/notion_sync/webhookevent/").status_code == 200
    detail = admin_client.get(change_url)
    assert detail.status_code == 200
    assert event.event_id in detail.content.decode()
    assert admin_client.post(change_url, {"type": "x"}).status_code == 403
    assert admin_client.get("/admin/notion_sync/webhookevent/add/").status_code == 403


def test_the_inline_mode_does_not_store_events(client, notion):
    page = notion.add_page()
    deliver(client, make_event(page["id"]))
    assert WebhookEvent.objects.count() == 0


def test_a_note_mapping_without_in_fields_still_gets_page_changed(
    client, notion, inbound_signals
):
    page = notion.add_page(data_source="ds-plain")
    note = Note.objects.create(text="hello")
    Note.objects.filter(pk=note.pk).update(
        notion_page_id=page["id"], notion_last_edited_time=EARLIER
    )

    deliver(
        client,
        make_event(page["id"], data_source="ds-plain"),
        token=PLAIN_WEBHOOK_TOKEN,
    )

    [(name, sender, kwargs)] = inbound_signals
    assert (sender, kwargs["instance"].pk, kwargs["changed_fields"]) == (
        Note,
        note.pk,
        [],
    )


@pytest.mark.skipif(
    django_settings.TEST_DB != "postgres", reason="needs row locks (Postgres)"
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_claims_skip_events_locked_by_another_run():
    for index in range(8):
        WebhookEvent.objects.create(
            event_id=f"event-{index}",
            type="page.created",
            entity_id="page",
            target="inbox",
        )
    ordered = WebhookEvent.objects.order_by("next_attempt_at", "received_at")
    claimed = []

    def other_run():
        try:
            claimed.extend(item.pk for item in webhooks.claim(5))
        finally:
            connection.close()

    with transaction.atomic():
        locked = [item.pk for item in ordered.select_for_update()[:5]]
        thread = threading.Thread(target=other_run)
        thread.start()
        thread.join(timeout=5)
        waited = thread.is_alive()
    thread.join(timeout=5)

    assert not waited, "claim() blocked on events locked by another transaction"
    assert len(claimed) == 3
    assert not set(claimed) & set(locked)


# --- The token page in the admin ---

TOKEN_PAGE = "/admin/notion_sync/webhookevent/verification-token/"


@pytest.fixture
def staff_client(client, django_user_model):
    """A staff user who may view webhook events but is not a superuser."""
    user = django_user_model.objects.create_user("staff", password="x", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="view_webhookevent"))
    client.force_login(user)
    return client


def test_a_superuser_sees_the_handshake_token(admin_client, client, caplog):
    caplog.set_level(logging.DEBUG)
    deliver(client, {"verification_token": "secret_from_notion"}, token=None)

    response = admin_client.get(TOKEN_PAGE)

    assert response.status_code == 200
    content = response.content.decode()
    assert "<code>secret_from_notion</code>" in content
    assert "WEBHOOK_VERIFICATION_TOKEN" in content
    assert "tokens arrived" not in content
    assert "secret_from_notion" not in caplog.text


def test_the_token_page_is_never_cached(admin_client):
    response = admin_client.get(TOKEN_PAGE)
    assert "no-store" in response["Cache-Control"]


def test_the_token_page_warns_about_several_tokens(admin_client, client):
    deliver(client, {"verification_token": "secret_one"}, token=None)
    deliver(client, {"verification_token": "secret_two"}, token=None)

    content = admin_client.get(TOKEN_PAGE).content.decode()

    assert "2 tokens arrived" in content
    assert "secret_one" in content and "secret_two" in content


def test_the_token_page_says_when_nothing_arrived(admin_client):
    content = admin_client.get(TOKEN_PAGE).content.decode()
    assert "Resend token" in content


def test_staff_who_are_not_superusers_cannot_see_the_token(staff_client, client):
    deliver(client, {"verification_token": "secret_hidden"}, token=None)

    response = staff_client.get(TOKEN_PAGE)

    assert response.status_code == 403
    assert b"secret_hidden" not in response.content
    listing = staff_client.get("/admin/notion_sync/webhookevent/")
    assert listing.status_code == 200
    assert TOKEN_PAGE.encode() not in listing.content


def test_anonymous_users_are_sent_to_the_login_page(client):
    deliver(client, {"verification_token": "secret_hidden"}, token=None)

    response = client.get(TOKEN_PAGE)

    assert response.status_code == 302
    assert "/admin/login/" in response["Location"]


def test_superusers_get_a_link_to_the_token_page(admin_client):
    listing = admin_client.get("/admin/notion_sync/webhookevent/")
    assert TOKEN_PAGE.encode() in listing.content
