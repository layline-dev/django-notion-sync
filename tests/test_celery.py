import logging
import subprocess
import sys
from datetime import timedelta

import pytest
from django.utils import timezone

from notion_sync import api, tasks
from notion_sync.models import OutboxItem
from tests.testapp.models import Ticket

pytestmark = pytest.mark.django_db

Status = OutboxItem.Status


@pytest.fixture
def celery_dispatch(settings):
    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "DISPATCH": "celery"}


def test_a_row_is_sent_by_a_task_once_it_commits(
    notion, celery_dispatch, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        item = api.enqueue_create("inbox", {"Name": "A"})
    assert notion.requests == []  # Nothing is sent before the commit.

    for callback in callbacks:
        callback()

    item.refresh_from_db()
    assert item.status == Status.SUCCEEDED
    assert notion.count("POST", "/pages") == 1


def test_a_model_sync_is_dispatched_too(
    notion, celery_dispatch, django_capture_on_commit_callbacks
):
    ticket = Ticket.objects.create(title="A")

    with django_capture_on_commit_callbacks(execute=True):
        item = ticket.enqueue_notion_sync()

    item.refresh_from_db()
    ticket.refresh_from_db()
    assert item.status == Status.SUCCEEDED
    assert ticket.notion_page_id == item.result_page_id


def test_nothing_is_dispatched_with_the_command(
    no_network, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks() as callbacks:
        api.enqueue_create("inbox", {"Name": "A"})

    assert callbacks == []


def test_a_task_that_cannot_be_queued_leaves_the_row_for_the_drain(
    no_network, celery_dispatch, django_capture_on_commit_callbacks, monkeypatch, caplog
):
    def broker_down(*args, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr(tasks.send_outbox_item, "delay", broker_down)

    with caplog.at_level(logging.WARNING, logger="notion_sync"):
        with django_capture_on_commit_callbacks(execute=True):
            item = api.enqueue_create("inbox", {"Name": "A"})

    item.refresh_from_db()
    assert item.status == Status.PENDING
    assert "the next drain will send it" in caplog.text


def test_send_outbox_item_sends_only_a_pending_due_row(notion):
    later = api.enqueue_create("inbox", {"Name": "later"})
    OutboxItem.objects.filter(pk=later.pk).update(
        next_attempt_at=timezone.now() + timedelta(minutes=5)
    )
    now = api.enqueue_create("inbox", {"Name": "now"})

    assert tasks.send_outbox_item.apply(args=[str(later.pk)]).get() is None
    assert tasks.send_outbox_item.apply(args=[str(now.pk)]).get() == "succeeded"
    assert tasks.send_outbox_item.apply(args=[str(now.pk)]).get() is None
    later.refresh_from_db()
    assert later.status == Status.PENDING


def test_drain_outbox_returns_counts(notion):
    api.enqueue_create("inbox", {"Name": "A"})

    assert tasks.drain_outbox.apply().get() == {"succeeded": 1}


CELERY_BLOCKED = """
import importlib, pkgutil, sys

sys.modules["celery"] = None  # any `import celery` now raises ImportError

import django

django.setup()

from django.core.management import call_command

import notion_sync

for module in pkgutil.walk_packages(notion_sync.__path__, "notion_sync."):
    if module.name != "notion_sync.tasks":
        importlib.import_module(module.name)
call_command("check")

try:
    import notion_sync.tasks
except ImportError:
    pass
else:
    raise AssertionError("celery was not blocked")
"""


def test_the_package_works_without_celery_installed():
    """Everything but notion_sync.tasks imports, and checks pass, with no Celery."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", CELERY_BLOCKED],
        env={"DJANGO_SETTINGS_MODULE": "tests.settings", "PYTHONPATH": "."},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
