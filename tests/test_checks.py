import sys

import pytest
from django.core.checks import run_checks

from tests.settings import INBOX_TOKEN, NOTION_SYNC
from tests.testapp.models import Unmapped

# ROOT_URLCONF for a project that does not include notion_sync.urls.
urlpatterns: list = []

DATABASE_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "notion_sync_test_cache",
    }
}


def messages():
    """What `manage.py check` would report from this package's checks."""
    return [
        message
        for message in run_checks(tags=["notion_sync"])
        if not message.is_silenced()
    ]


def ids():
    return sorted(message.id for message in messages())


def with_inbox(settings, raw):
    """Replace the "inbox" target, keeping the others the test app maps to."""
    targets = {**NOTION_SYNC["TARGETS"], "inbox": raw}
    settings.NOTION_SYNC = {**NOTION_SYNC, "TARGETS": targets}


def test_the_test_project_passes():
    assert ids() == []


def test_an_invalid_setting_is_one_error(settings):
    settings.NOTION_SYNC = {"TARGETS": {}, "DISPATCH": "cron"}

    [message] = messages()

    assert message.id == "notion_sync.E001"
    assert "DISPATCH" in message.msg
    assert message.hint


def test_a_malformed_target_is_an_error(settings):
    with_inbox(
        settings, {"TOKEN": INBOX_TOKEN, "DATA_SOURCE_ID": "ds", "COLOUR": "red"}
    )

    [message] = messages()

    assert message.id == "notion_sync.E002"
    assert "COLOUR" in message.msg
    assert INBOX_TOKEN not in message.msg


def test_a_target_that_is_not_a_dict_is_an_error(settings):
    with_inbox(settings, "ds-inbox")

    assert ids() == ["notion_sync.E002"]


def test_unset_credentials_are_warnings(settings):
    with_inbox(settings, {"TOKEN": None, "DATA_SOURCE_ID": "  "})

    found = messages()

    assert [message.id for message in found] == ["notion_sync.W001"] * 2
    assert "TOKEN" in found[0].msg
    assert "DATA_SOURCE_ID" in found[1].msg


def test_the_rest_of_a_target_is_checked_when_a_credential_is_unset(settings):
    with_inbox(settings, {"DATA_SOURCE_ID": "ds", "COLOUR": "red"})

    assert ids() == ["notion_sync.E002", "notion_sync.W001"]


def test_a_mapping_to_an_unknown_target_is_an_error(register):
    register(Unmapped, target="missing")

    [message] = messages()

    assert message.id == "notion_sync.E003"
    assert message.obj is Unmapped
    assert "'missing'" in message.msg


def test_mappings_are_not_checked_when_the_setting_is_invalid(settings, register):
    register(Unmapped, target="missing")
    settings.NOTION_SYNC = {"TARGETS": []}

    assert ids() == ["notion_sync.E001"]


def test_celery_dispatch_without_celery_is_an_error(settings, monkeypatch):
    settings.NOTION_SYNC = {**NOTION_SYNC, "DISPATCH": "celery"}
    assert ids() == []

    monkeypatch.setitem(sys.modules, "celery", None)

    assert ids() == ["notion_sync.E004"]


@pytest.mark.parametrize(
    ("backend", "problem"),
    [
        ("django.core.cache.backends.locmem.LocMemCache", "keeps entries in one"),
        ("django.core.cache.backends.dummy.DummyCache", "stores nothing"),
    ],
)
def test_webhooks_with_an_unshared_cache_warn(settings, backend, problem):
    settings.SILENCED_SYSTEM_CHECKS = []
    settings.CACHES = {"default": {"BACKEND": backend}}

    [message] = messages()

    assert message.id == "notion_sync.W002"
    assert problem in message.msg


def test_webhooks_with_a_shared_cache_pass(settings):
    settings.SILENCED_SYSTEM_CHECKS = []
    settings.CACHES = DATABASE_CACHE

    assert ids() == []


@pytest.mark.parametrize("urlconf", ["tests.test_checks", "tests.no_such_urlconf"])
def test_the_cache_is_not_checked_without_the_webhook_url(settings, urlconf):
    settings.SILENCED_SYSTEM_CHECKS = []
    settings.ROOT_URLCONF = urlconf

    assert ids() == []
