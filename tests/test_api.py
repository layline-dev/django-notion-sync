from datetime import UTC, date, datetime
from io import BytesIO

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import ContentFile

from notion_sync import api, files
from notion_sync.exceptions import (
    NotionNotFoundError,
    NotionRequestValidationError,
    NotionValidationError,
)
from notion_sync.files import NotionFile
from notion_sync.signals import page_synced
from tests.fake_notion import error_response


def text(content):
    return {"rich_text": [{"type": "text", "text": {"content": content}}]}


def title(content):
    return {"title": [{"type": "text", "text": {"content": content}}]}


def test_create_page_returns_plain_values(notion, captured_signals):
    page = api.create_page(
        "inbox",
        {
            "Name": "Checkout button does nothing",
            "Select": "High",
            "Due": date(2026, 12, 1),
            "Tags": ["bug", "web"],
            "Done": False,
        },
    )

    assert page.id in notion.pages
    assert page.url.startswith("https://www.notion.so/")
    assert page.last_edited_time == datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
    assert page.in_trash is False
    assert page.properties == {
        "Name": "Checkout button does nothing",
        "Select": "High",
        "Due": date(2026, 12, 1),
        "Tags": ["bug", "web"],
        "Done": False,
    }
    [(name, sent)] = captured_signals
    assert name == "page_synced"
    assert sent["target"] == "inbox"
    assert sent["operation"] == "create"
    assert sent["page_id"] == page.id
    assert sent["page"] == page
    assert sent["item"] is None


def test_create_page_writes_the_external_id(notion):
    page = api.create_page("inbox", {"Name": "A"}, external_id="order-17")

    assert page.properties["External ID"] == "order-17"


def test_external_id_needs_a_target_that_supports_it(notion):
    with pytest.raises(NotionValidationError, match="EXTERNAL_ID_PROPERTY"):
        api.create_page("plain", {"Name": "A"}, external_id="order-17")

    assert notion.requests == []


def test_unknown_target_is_reported_before_any_request(notion):
    with pytest.raises(ImproperlyConfigured):
        api.create_page("nope", {"Name": "A"})

    assert notion.requests == []


def test_a_mismatch_is_raised_before_any_page_request(notion, captured_signals):
    with pytest.raises(NotionValidationError) as raised:
        api.create_page("inbox", {"Name": "A", "Number": "five"})

    assert raised.value.property == "Number"
    assert notion.count("POST", "/pages") == 0
    assert captured_signals == []


def test_schema_is_cached_between_calls(notion):
    api.create_page("inbox", {"Name": "A"})
    api.create_page("inbox", {"Name": "B"})

    assert notion.count("GET", "/data_sources/") == 1


def test_schema_cache_can_be_turned_off(notion, settings):
    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "SCHEMA_CACHE_SECONDS": 0}

    api.create_page("inbox", {"Name": "A"})
    api.create_page("inbox", {"Name": "B"})

    assert notion.count("GET", "/data_sources/") == 2


def test_a_new_property_is_found_by_refreshing_a_stale_schema(notion):
    api.create_page("inbox", {"Name": "A"})  # Caches the schema.
    notion.schemas["ds-inbox"]["Severity"] = "select"

    page = api.create_page("inbox", {"Name": "B", "Severity": "S1"})

    assert page.properties["Severity"] == "S1"
    assert notion.count("GET", "/data_sources/") == 2


def test_notions_validation_error_refreshes_the_schema_and_retries_once(notion):
    api.create_page("inbox", {"Name": "A"})  # Caches the schema.
    notion.schemas["ds-inbox"]["Text"] = "number"  # Retyped in Notion.

    with pytest.raises(NotionValidationError) as raised:
        api.create_page("inbox", {"Name": "B", "Text": "words"})

    # Notion rejected the stale payload, the schema was fetched again, and
    # the value then failed locally against the real type.
    assert raised.value.notion_type == "number"
    assert notion.count("POST", "/pages") == 2
    assert notion.count("GET", "/data_sources/") == 2


def test_a_retyped_property_is_sent_correctly_after_the_refresh(notion):
    api.create_page("inbox", {"Name": "A"})
    notion.schemas["ds-inbox"]["Text"] = "title"
    notion.schemas["ds-inbox"]["Name"] = "rich_text"

    page = api.create_page("inbox", {"Name": "B", "Text": "T"})

    assert notion.count("POST", "/pages") == 3
    assert notion.last_json("POST", "/pages")["properties"] == {
        "Name": text("B"),
        "Text": title("T"),
    }
    assert page.properties == {"Name": "B", "Text": "T"}


def test_notions_validation_error_is_not_retried_with_a_fresh_schema(notion):
    notion.fail_next("POST", "/pages", error_response(400, "validation_error"))

    with pytest.raises(NotionRequestValidationError):
        api.create_page("inbox", {"Name": "A"})

    assert notion.count("POST", "/pages") == 1


def test_update_page(notion, captured_signals):
    existing = notion.add_page({"Name": title("Old"), "Number": {"number": 1}})

    page = api.update_page("inbox", existing["id"], {"Name": "New"})

    assert page.properties == {"Name": "New", "Number": 1}
    assert notion.last_json("PATCH", "/pages/") == {
        "properties": {"Name": title("New")}
    }
    assert captured_signals[0][1]["operation"] == "update"


def test_update_of_a_missing_page_is_not_found(notion):
    with pytest.raises(NotionNotFoundError):
        api.update_page("inbox", "missing", {"Name": "New"})


def test_upsert_creates_when_no_page_has_the_external_id(notion):
    page = api.upsert_page("inbox", "order-17", {"Name": "A"})

    assert page.properties == {"Name": "A", "External ID": "order-17"}
    assert notion.last_json("POST", "/query") == {
        "filter": {"property": "External ID", "rich_text": {"equals": "order-17"}},
        "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
        "page_size": 2,
    }


def test_upsert_updates_the_page_with_the_external_id(notion, captured_signals):
    existing = notion.add_page({"Name": title("Old"), "External ID": text("order-17")})
    notion.add_page({"Name": title("Other"), "External ID": text("order-18")})

    page = api.upsert_page("inbox", "order-17", {"Name": "New"})

    assert page.id == existing["id"]
    assert page.properties["Name"] == "New"
    assert notion.count("POST", "/pages") == 0
    assert len(notion.pages) == 2
    assert captured_signals[0][1]["operation"] == "upsert"


def test_upsert_ignores_pages_in_the_trash(notion):
    trashed = notion.add_page({"External ID": text("order-17")})
    trashed["in_trash"] = True

    page = api.upsert_page("inbox", "order-17", {"Name": "New"})

    assert page.id != trashed["id"]


def test_upsert_needs_a_text_external_id_property(notion):
    notion.schemas["ds-inbox"]["External ID"] = "number"

    with pytest.raises(NotionValidationError, match="cannot hold the external ID"):
        api.upsert_page("inbox", "order-17", {"Name": "A"})

    assert notion.count("POST", "/query") == 0


def test_archive_page_moves_it_to_the_trash(notion, captured_signals):
    existing = notion.add_page({"Name": title("A")})

    assert api.archive_page("inbox", existing["id"]) is None

    assert notion.pages[existing["id"]]["in_trash"] is True
    assert notion.last_json("PATCH", "/pages/") == {"in_trash": True}
    [(_, sent)] = captured_signals
    assert sent["operation"] == "archive"
    assert sent["page"].in_trash is True


def test_get_page(notion, captured_signals):
    existing = notion.add_page({"Name": title("A"), "Done": {"checkbox": True}})

    page = api.get_page("inbox", existing["id"])

    assert page.id == existing["id"]
    assert page.properties == {"Name": "A", "Done": True}
    assert captured_signals == []


def test_a_failing_signal_receiver_does_not_fail_the_write(notion):
    def broken(sender, **kwargs):
        raise RuntimeError("receiver bug")

    page_synced.connect(broken)
    try:
        page = api.create_page("inbox", {"Name": "A"})
    finally:
        page_synced.disconnect(broken)

    assert page.id in notion.pages


# --- Files ---


def test_a_file_is_uploaded_and_attached(notion, storage):
    storage.save("shots/shot.png", BytesIO(b"\x89PNG-bytes"))

    api.create_page("inbox", {"Name": "A", "Files": NotionFile("shots/shot.png")})

    [(upload_id, upload)] = notion.uploads.items()
    assert upload["request"] == {
        "filename": "shot.png",
        "content_type": "image/png",
        "mode": "single_part",
    }
    [part] = upload["parts"]
    assert b"\x89PNG-bytes" in part
    assert b'filename="shot.png"' in part
    assert upload["completed"] is False
    assert notion.last_json("POST", "/pages")["properties"]["Files"] == {
        "files": [
            {
                "type": "file_upload",
                "file_upload": {"id": upload_id},
                "name": "shot.png",
            }
        ]
    }


def test_an_in_memory_file_can_be_sent_inline(notion):
    upload = ContentFile(b"hello", name="note.txt")

    api.create_page("inbox", {"Name": "A", "Files": [NotionFile(upload)]})

    [stored] = notion.uploads.values()
    assert stored["request"]["filename"] == "note.txt"
    assert b"hello" in stored["parts"][0]


def test_a_large_file_is_sent_in_parts(notion, storage, monkeypatch):
    monkeypatch.setattr(files, "SINGLE_PART_LIMIT", 10)
    monkeypatch.setattr(files, "PART_SIZE", 4)
    storage.save("big.bin", BytesIO(b"0123456789AB"))

    api.create_page("inbox", {"Name": "A", "Files": NotionFile("big.bin")})

    [upload] = notion.uploads.values()
    assert upload["request"]["mode"] == "multi_part"
    assert upload["request"]["number_of_parts"] == 3
    assert [b"0123" in upload["parts"][0], b"4567" in upload["parts"][1]] == [
        True,
        True,
    ]
    assert b"89AB" in upload["parts"][2]
    for number, part in enumerate(upload["parts"], start=1):
        assert f'name="part_number"\r\n\r\n{number}\r\n'.encode() in part
    assert upload["completed"] is True


def test_nothing_is_uploaded_when_another_value_is_invalid(notion, storage):
    storage.save("shot.png", BytesIO(b"png"))

    with pytest.raises(NotionValidationError):
        api.create_page(
            "inbox", {"Name": "A", "Files": NotionFile("shot.png"), "Done": "yes"}
        )

    assert notion.uploads == {}


def test_a_file_is_uploaded_once_when_the_schema_is_refreshed(notion, storage):
    storage.save("shot.png", BytesIO(b"png"))
    api.create_page("inbox", {"Name": "A"})
    notion.schemas["ds-inbox"]["Text"] = "title"
    notion.schemas["ds-inbox"]["Name"] = "rich_text"

    api.create_page("inbox", {"Name": "B", "Files": NotionFile("shot.png")})

    assert len(notion.uploads) == 1


def test_a_missing_file_is_a_validation_error(notion, storage):
    with pytest.raises(NotionValidationError, match="not found in storage"):
        api.create_page("inbox", {"Name": "A", "Files": NotionFile("gone.png")})

    assert notion.count("POST", "/pages") == 0
