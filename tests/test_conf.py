import pytest
from django.core.exceptions import ImproperlyConfigured

from notion_sync.conf import NOTION_VERSION, Target, get_settings, get_target
from tests.settings import INBOX_TOKEN, INBOX_WEBHOOK_TOKEN


def test_defaults_are_filled_in():
    settings = get_settings()

    assert settings.notion_version == NOTION_VERSION == "2025-09-03"
    assert settings.max_attempts == 8
    assert settings.retention_days == 7
    assert settings.schema_cache_seconds == 300
    assert settings.dispatch == "command"
    assert settings.auto_sync is False


def test_missing_setting_means_no_targets(settings):
    del settings.NOTION_SYNC

    assert get_settings().targets == {}


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"TYPO": 1}, "unknown keys: TYPO"),
        ({"DISPATCH": "threads"}, "DISPATCH"),
        ({"MAX_ATTEMPTS": 0}, "MAX_ATTEMPTS"),
        ({"MAX_ATTEMPTS": True}, "MAX_ATTEMPTS"),
        ({"RETENTION_DAYS": -1}, "RETENTION_DAYS"),
        ({"AUTO_SYNC": "yes"}, "AUTO_SYNC"),
        ({"TARGETS": []}, "TARGETS"),
        ({"NOTION_VERSION": ""}, "NOTION_VERSION"),
    ],
)
def test_invalid_settings_are_rejected(settings, override, message):
    settings.NOTION_SYNC = override

    with pytest.raises(ImproperlyConfigured, match=message):
        get_settings()


def test_get_target_reads_a_named_target():
    target = get_target("inbox")

    assert target == Target(
        name="inbox",
        token=INBOX_TOKEN,
        data_source_id="ds-inbox",
        external_id_property="External ID",
        webhook_verification_token=INBOX_WEBHOOK_TOKEN,
    )
    assert get_target("plain").external_id_property is None


def test_get_target_returns_a_target_object_unchanged():
    target = Target(name="runtime", token="t", data_source_id="ds")

    assert get_target(target) is target


def test_unknown_target_is_an_error():
    with pytest.raises(ImproperlyConfigured, match="no target named 'nope'"):
        get_target("nope")


def test_a_broken_target_does_not_break_the_others(settings):
    settings.NOTION_SYNC = {
        "TARGETS": {
            "good": {"TOKEN": "t", "DATA_SOURCE_ID": "ds"},
            "bad": {"TOKEN": None, "DATA_SOURCE_ID": "ds"},
        }
    }

    assert get_target("good").data_source_id == "ds"
    with pytest.raises(ImproperlyConfigured, match="needs a non-empty TOKEN"):
        get_target("bad")


def test_unknown_target_key_is_rejected(settings):
    settings.NOTION_SYNC = {
        "TARGETS": {"t": {"TOKEN": "t", "DATA_SOURCE_ID": "ds", "DATABASE_ID": "x"}}
    }

    with pytest.raises(ImproperlyConfigured, match="unknown keys: DATABASE_ID"):
        get_target("t")


def test_tokens_stay_out_of_reprs_and_errors(settings):
    target = Target(
        name="t",
        token="tok-secret",
        data_source_id="ds",
        webhook_verification_token="hook-secret",
    )
    assert "secret" not in repr(target)

    settings.NOTION_SYNC = {
        "TARGETS": {"t": {"TOKEN": "tok-secret", "DATA_SOURCE_ID": ""}}
    }
    assert "tok-secret" not in repr(get_settings())
    with pytest.raises(ImproperlyConfigured) as raised:
        get_target("t")
    assert "tok-secret" not in str(raised.value)
