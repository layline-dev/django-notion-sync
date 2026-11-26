import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from notion_sync import api, outbox
from notion_sync.models import OutboxItem
from tests.testapp.models import Ticket

pytestmark = pytest.mark.django_db

Status = OutboxItem.Status
CHANGELIST = "admin:notion_sync_outboxitem_changelist"


@pytest.fixture
def admin_client(client):
    user = User.objects.create_superuser("admin", "admin@example.com", "pw")
    client.force_login(user)
    return client


def test_the_changelist_shows_rows_and_filters(admin_client, no_network):
    item = api.enqueue_create("inbox", {"Name": "A"})
    OutboxItem.objects.filter(pk=item.pk).update(
        status=Status.FAILED, last_error="Notion said no"
    )

    response = admin_client.get(reverse(CHANGELIST), {"status__exact": "failed"})

    assert response.status_code == 200
    assert "Notion said no" in response.text
    assert admin_client.get(reverse(CHANGELIST), {"q": str(item.pk)}).status_code == 200
    # Text that is not a UUID must not break the exact ID search.
    assert admin_client.get(reverse(CHANGELIST), {"q": "said no"}).status_code == 200


def test_the_detail_page_is_read_only(admin_client, no_network):
    item = api.enqueue_create("inbox", {"Name": "Secretless <b>payload</b>"})
    url = reverse("admin:notion_sync_outboxitem_change", args=[item.pk])

    response = admin_client.get(url)

    assert response.status_code == 200
    assert "&lt;b&gt;payload&lt;/b&gt;" in response.text  # Escaped.
    assert 'name="payload"' not in response.text  # Not an editable input.
    assert (
        admin_client.get(reverse("admin:notion_sync_outboxitem_add")).status_code == 403
    )


def test_a_model_row_shows_its_record(admin_client, no_network):
    ticket = Ticket.objects.create(title="A")
    item = ticket.enqueue_notion_sync()

    response = admin_client.get(
        reverse("admin:notion_sync_outboxitem_change", args=[item.pk])
    )

    assert f"Testapp | ticket {ticket.pk}" in response.text
    assert "values are read from the record when sent" in response.text


def test_the_retry_action_queues_parked_rows_again(admin_client, notion):
    failed = api.enqueue_create("inbox", {"Name": "A"})
    review = api.enqueue_create("plain", {"Name": "B"})
    sent = api.enqueue_create("inbox", {"Name": "C"})
    OutboxItem.objects.filter(pk=failed.pk).update(
        status=Status.FAILED, attempts=8, last_error="Gave up"
    )
    OutboxItem.objects.filter(pk=review.pk).update(status=Status.NEEDS_REVIEW)
    OutboxItem.objects.filter(pk=sent.pk).update(status=Status.SUCCEEDED)

    response = admin_client.post(
        reverse(CHANGELIST),
        {
            "action": "retry",
            "_selected_action": [str(failed.pk), str(review.pk), str(sent.pk)],
        },
        follow=True,
    )

    assert "2 row(s) queued to send again." in response.text
    assert "1 row(s) left alone" in response.text
    failed.refresh_from_db()
    assert (failed.status, failed.attempts) == (Status.PENDING, 0)
    assert failed.last_error == "Gave up"  # Kept, and arms the duplicate guard.
    sent.refresh_from_db()
    assert sent.status == Status.SUCCEEDED


def test_a_retried_create_looks_for_the_page_it_may_have_made(admin_client, notion):
    item = api.enqueue_create("inbox", {"Name": "A"})
    notion.add_page(
        {
            "Name": {"title": []},
            "External ID": {
                "rich_text": [{"type": "text", "text": {"content": str(item.pk)}}]
            },
        }
    )
    OutboxItem.objects.filter(pk=item.pk).update(
        status=Status.FAILED, attempts=8, last_error="Gave up"
    )

    admin_client.post(
        reverse(CHANGELIST), {"action": "retry", "_selected_action": [str(item.pk)]}
    )

    assert outbox.drain() == {"succeeded": 1}
    assert notion.count("POST", "/pages") == 0


def test_retry_reports_rows_replaced_by_a_newer_one(admin_client, no_network):
    ticket = Ticket.objects.create(title="A")
    first = ticket.enqueue_notion_sync()
    OutboxItem.objects.filter(pk=first.pk).update(status=Status.FAILED)
    ticket.enqueue_notion_sync()

    response = admin_client.post(
        reverse(CHANGELIST),
        {"action": "retry", "_selected_action": [str(first.pk)]},
        follow=True,
    )

    assert "a newer row for the same record is already queued" in response.text
