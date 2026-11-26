from datetime import timedelta
from io import StringIO

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from notion_sync import In, Out, api
from notion_sync.models import OutboxItem
from tests.fake_notion import error_response
from tests.settings import INBOX_TOKEN, PLAIN_TOKEN
from tests.testapp.models import Ticket, Unmapped


def run(*args):
    out = StringIO()
    call_command(*args, stdout=out, no_color=True)
    return out.getvalue()


@pytest.mark.django_db
def test_drain_command_reports_what_happened(notion):
    assert run("notion_sync_drain") == "Nothing to send.\n"

    api.enqueue_create("plain", {"Name": "A"})
    api.enqueue_create("plain", {"Name": "B"})
    api.enqueue_create("plain", {"Name": "C", "Number": "bad"})
    notion.fail_next("POST", "/pages", httpx.ConnectError("refused"))

    assert run("notion_sync_drain") == "1 sent, 1 will retry, 1 failed\n"


@pytest.mark.django_db
def test_drain_command_reports_skipped_rows(no_network):
    ticket = Ticket.objects.create(title="A")
    ticket.enqueue_notion_sync()
    ticket.delete()

    assert run("notion_sync_drain") == "1 skipped\n"


@pytest.mark.django_db
def test_drain_command_limit(notion):
    for name in "ABC":
        api.enqueue_create("plain", {"Name": name})

    assert run("notion_sync_drain", "--limit", "2") == "2 sent\n"
    assert OutboxItem.objects.filter(status="pending").count() == 1


@pytest.mark.django_db
def test_prune_command(no_network):
    item = api.enqueue_create("plain", {"Name": "A"})
    OutboxItem.objects.filter(pk=item.pk).update(
        status="succeeded", updated_at=timezone.now() - timedelta(days=3)
    )

    assert (
        run("notion_sync_prune")
        == "Deleted 0 sent rows and 0 processed webhook events.\n"
    )
    assert (
        run("notion_sync_prune", "--days", "2")
        == "Deleted 1 sent rows and 0 processed webhook events.\n"
    )


def test_check_command_passes_for_healthy_targets(notion):
    output = run("notion_sync_check")

    assert output == (
        "inbox:\n"
        "  ok    Token accepted.\n"
        "  ok    Data source is shared with the integration.\n"
        '  ok    External ID property "External ID" exists (rich_text).\n'
        "plain:\n"
        "  ok    Token accepted.\n"
        "  ok    Data source is shared with the integration.\n"
        "testapp.Note -> plain:\n"
        '  ok    "Name" (title) from text\n'
        '  ok    "Number" (number) from word_count\n'
        "testapp.Ticket -> inbox:\n"
        '  ok    "Name" (title) from title\n'
        '  ok    "Text" (rich_text) from body\n'
        '  ok    "Select" (select) from priority\n'
        '  ok    "Tags" (multi_select) from tags\n'
        '  ok    "Due" (date) from due\n'
        '  ok    "Done" (checkbox) from done\n'
        '  ok    "Link" (url) from link\n'
        '  ok    "Files" (files) from screenshot\n'
        '  ok    "Status" (status) into status\n'
        "All targets are ready.\n"
    )


def check_fails(*args):
    out = StringIO()
    with pytest.raises(CommandError) as raised:
        call_command("notion_sync_check", *args, stdout=out, no_color=True)
    return out.getvalue(), str(raised.value)


def test_check_command_reports_a_rejected_token(notion):
    notion.fail_next(
        "GET", "/data_sources/ds-inbox", error_response(401, "unauthorized")
    )

    output, error = check_fails()

    assert "inbox:\n  FAIL  Notion rejected the token.\n" in output
    assert "plain:\n  ok    Token accepted.\n" in output
    assert "testapp.Ticket -> inbox:\n  FAIL  The target is not ready" in output
    assert error == "2 problem(s) found."
    assert INBOX_TOKEN not in output
    assert PLAIN_TOKEN not in output


def test_check_command_reports_an_unshared_data_source(notion):
    del notion.schemas["ds-plain"]

    output, error = check_fails("--target", "plain")

    assert output == (
        "plain:\n"
        "  ok    Token accepted.\n"
        "  FAIL  Data source not found. Check DATA_SOURCE_ID and that the "
        "database is shared with the integration.\n"
        "testapp.Note -> plain:\n"
        "  FAIL  The target is not ready; see above.\n"
    )
    assert error == "2 problem(s) found."


def test_check_command_reports_external_id_property_problems(notion):
    del notion.schemas["ds-inbox"]["External ID"]
    output, _ = check_fails("--target", "inbox")
    assert 'FAIL  External ID property "External ID" does not exist' in output

    notion.schemas["ds-inbox"]["External ID"] = "number"
    output, _ = check_fails("--target", "inbox")
    assert "is a number property; it must be text." in output


def test_check_command_reports_configuration_and_network_problems(notion, settings):
    settings.NOTION_SYNC = {
        "TARGETS": {
            "broken": {"TOKEN": "", "DATA_SOURCE_ID": "ds-inbox"},
            "down": {"TOKEN": "t", "DATA_SOURCE_ID": "ds-plain"},
        }
    }
    notion.fail_next("GET", "/data_sources/ds-plain", httpx.ConnectError("refused"))

    output, error = check_fails()

    assert "needs a non-empty TOKEN" in output
    assert "Could not reach the data source: Could not connect to Notion." in output
    # The test app's mappings point at targets this setting does not have.
    assert (
        "testapp.Ticket -> inbox:\n"
        "  FAIL  NOTION_SYNC['TARGETS'] has no target named 'inbox'.\n"
    ) in output
    assert error == "4 problem(s) found."


def test_check_command_needs_targets(settings):
    settings.NOTION_SYNC = {}

    with pytest.raises(CommandError, match="TARGETS"):
        call_command("notion_sync_check")


def test_check_command_reports_mappings_that_do_not_fit(notion, register):
    notion.schemas["ds-inbox"]["Done"] = "rich_text"
    notion.schemas["ds-inbox"]["Status"] = "number"
    del notion.schemas["ds-inbox"]["Link"]
    register(
        Unmapped,
        fields={
            "External ID": Out(lambda u: u.name),
            "Score": Out(lambda u: 1),
            "Ticket": In("name"),
        },
    )

    output, error = check_fails("--target", "inbox")

    assert (
        '  FAIL  "Done" is a rich_text property; it cannot be filled from '
        "Ticket.done (BooleanField).\n"
    ) in output
    assert (
        '  FAIL  "Status" is a number property; it cannot be filled into '
        "Ticket.status (CharField).\n"
    ) in output
    assert '  FAIL  "Link" does not exist in the data source.\n' in output
    assert (
        "testapp.Unmapped -> inbox:\n"
        '  FAIL  "External ID" is the target\'s external ID property; the '
        "package writes it, so do not map it.\n"
        '  FAIL  "Score" is a formula property, which is read-only.\n'
        '  ok    "Ticket" (unique_id) into name\n'
    ) in output
    assert "testapp.Note" not in output  # Its target was not asked for.
    assert error == "5 problem(s) found."
